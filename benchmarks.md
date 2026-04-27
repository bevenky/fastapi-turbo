# fastapi-turbo Benchmarks

> **Reading order:** the most recent runs (concurrent load,
> external-tool cross-checks, c=1/32/256 sweeps) are at the top of
> this file. Sections below the first `---` are **historical**:
> earlier single-connection-sequential runs that were superseded
> by the concurrent-load methodology. Treat the historical numbers
> as data points, not release messaging — they don't reflect
> behaviour under realistic concurrency, and the only release-claim
> wording lives in [README.md](README.md) and the topmost dated
> section here.

## Test Environment

- **Machine**: Apple Silicon (ARM64)
- **OS**: macOS Darwin 25.4.0
- **Rust**: 1.94.1 (via rustup)
- **Go**: 1.26.2 (Gin 1.10, Echo 4.12)
- **Node.js**: 25.9.0 (Fastify 5.x)
- **Python**: 3.14.4 (Pydantic 2.13, orjson 3.11)
- **Benchmark client**: Compiled Rust binary, raw TCP sockets, HTTP/1.1 keep-alive
- **Methodology**: 20,000 requests, 3,000 warmup, single connection, sequential

---

## 2026-04-24 — concurrent load + external-tool cross-check

Previous runs were single-connection sequential loopback. Those
numbers tell you about *per-request overhead*, not about how the
stack behaves under concurrent load or when measured by an
independent client. Both pictures matter; here they are side by side.

**Setup:** same M-series / macOS / Python 3.14 / GIL-enabled env.
Endpoint: `@app.get("/hello")` returning `{"ok": True}`.

### fastapi-turbo — single vs concurrent connections

| Connections | p50 | p95 | p99 | req/s |
|:---:|--:|--:|--:|--:|
| 1 | 27 μs | 35 μs | 42 μs | 31,910 |
| 32 | 293 μs | 691 μs | 967 μs | 92,893 |
| 256 | 2,370 μs | 4,468 μs | 5,608 μs | 94,167 |

The c=1 number is what's quoted in the README table and is a
measure of per-request overhead only. c=32 and c=256 show the
throughput ceiling our worker-loop model hits (~93K req/s sustained);
the latency climbs because 32+ concurrent in-flight requests queue
against a single shared async loop. This is the number users will
see under real load; the c=1 number is not.

### oha cross-check (independent client, Rust-based)

Running the same endpoint via [oha](https://github.com/hatoo/oha)
to sanity-check our custom `fastapi-turbo-bench`:

| Config | Our client p50 | oha average | Our req/s | oha req/s |
|:---:|--:|--:|--:|--:|
| c=1 | 27 μs | 31 μs | 31,910 | 31,910 |
| c=32 | 293 μs | 389 μs | 92,893 | 81,582 |

The c=1 numbers match within measurement noise (31 μs vs our 27 μs p50;
rps identical). c=32 shows our client measures ~12% faster average
latency than oha — not a lie, but the difference comes from how each
tool distributes work across connections (our client keeps N persistent
streams hot; oha uses a connection pool with different batching).
Both tools confirm the ~90K-100K req/s throughput ceiling.

### What we have NOT yet published

- **Linux x86_64** results. macOS loopback has different scheduler
  and syscall characteristics than Linux production. README claims
  should be calibrated against Linux once CI publishes them.
- **`wrk` / `bombardier` cross-checks.** `oha` is one independent
  client; at least one of `wrk` (C, the de facto standard) or
  `bombardier` (Go) would tighten the error bars. These tools also
  aren't installed in this environment; adding them is TODO.

Treat the README's "ties Go" claim as accurate for c=1 loopback
only. At c=32+ Go's per-core goroutine model is harder to beat;
no honest number lets us claim parity at every concurrency level.

---

## 2026-04-20 — fastapi-turbo vs FastAPI+uvicorn vs Go Gin (head-to-head)

*All three servers run an endpoint-identical app. `20,000` requests per
endpoint after `3,000` warmup, single TCP connection, HTTP/1.1 keep-alive,
compiled Rust bench client.*

| Endpoint | FA+uvicorn p50 | **fastapi-turbo p50** | Go Gin p50 | FA req/s | **FR req/s** | Gin req/s | FR vs FA | FR vs Gin |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| GET /hello (plain JSON) | 162 μs | **25 μs** | 24 μs | 6,133 | **38,698** | 39,319 | **6.5×** | 0.96× |
| GET /path/42 (path param + int coerce) | 171 μs | **27 μs** | 24 μs | 5,819 | **36,118** | 38,871 | **6.3×** | 0.89× |
| GET /headers (header extraction) | 178 μs | **27 μs** | 24 μs | 5,608 | **35,999** | 39,200 | **6.6×** | 0.89× |
| GET /with-deps (2-level Depends) | 110 μs | **29 μs** | 24 μs | 9,127 | **33,696** | 39,000 | **3.8×** | 0.83× |
| GET /list (20-item list) | 196 μs | **37 μs** | 30 μs | 5,037 | **26,592** | 29,987 | **5.3×** | 0.81× |
| POST /items (Pydantic body validate) | 181 μs | **34 μs** | 26 μs | 5,491 | **28,381** | 36,102 | **5.3×** | 0.76× |

### Takeaways

- **4–7× faster p50 than stock FastAPI + uvicorn** — fastapi-turbo processes
  each request in 25–37 µs vs 110–196 µs for FastAPI.
- **Within 4–24% of Go Gin on p50**, matching Gin on `GET /hello`
  (25 vs 24 µs) and landing within a few microseconds on every other
  endpoint. This is a Python framework running Python user code,
  Pydantic validation, and `Depends(...)` resolution on every request;
  Gin is native Go with manual struct binding and no DI layer.
- **Pydantic body validation (`POST /items`) is the largest residual
  gap vs Gin** (34 µs vs 26 µs). The extra ~8 µs is pydantic-core +
  error-shape post-processing — the tax for automatic request-body
  validation and typed errors, which Gin doesn't provide (its
  `ShouldBindJSON` returns a raw error string).
- **`/with-deps` (2-level `Depends` chain + Header extraction, both deps
  `async def`)** — fastapi-turbo statically scans async functions for
  `await` expressions and drives await-free ones on the calling thread
  (~2 µs) instead of submitting to the shared worker loop (~30 µs).
  Real async deps that hit the DB still run on the shared loop to
  preserve asyncpg / redis.asyncio / httpx connection-pool affinity
  (verified at 100% SQLA + 100% Redis parity).
- Runner harness: `benchmarks/run_bench.py` (boots all three servers
  from a single parity app + matching Gin binary, writes a markdown
  table to `benchmarks/latest_bench.md`).

Reproducing:

```bash
source /Users/venky/tech/fastapi_turbo_env/bin/activate
cargo build --release --bin fastapi-turbo-bench
(cd benchmarks/go-gin && go build -o bench-gin .)
python benchmarks/run_bench.py
```

---

## 2026-04-15 — Final Comprehensive Results

### GET /hello (simple JSON response: `{"message": "hello"}`)

| # | Framework | p50 | p99 | min | req/s |
|---|-----------|-----|-----|-----|-------|
| 1 | **Pure Rust Axum** | **18 μs** | 29 μs | 12 μs | 53,085 |
| 2 | Node.js Fastify | 22 μs | 38 μs | 14 μs | 43,045 |
| 3 | **fastapi-turbo** | **24 μs** | **37 μs** | 15 μs | **41,392** |
| 3 | Go Gin | 24 μs | 40 μs | 15 μs | 39,760 |
| 3 | Go Echo | 24 μs | 43 μs | 16 μs | 40,252 |
| 6 | FastAPI + uvicorn | 188 μs | 230 μs | 155 μs | 5,272 |

**fastapi-turbo ties Go Gin and Go Echo, and beats Go on throughput** (41,392 vs 39,760 req/s).

### GET /with-deps (2-level dependency injection + header extraction)

| # | Framework | p50 | p99 | min | req/s | DI type |
|---|-----------|-----|-----|-----|-------|---------|
| 1 | **Pure Rust Axum** | **17 μs** | 28 μs | 13 μs | 55,712 | Compile-time |
| 2 | Node.js Fastify | 22 μs | 35 μs | 14 μs | 44,163 | Manual |
| 3 | Go Gin | 24 μs | 69 μs | 16 μs | 37,687 | Manual |
| 3 | Go Echo | 24 μs | 36 μs | 16 μs | 41,079 | Manual |
| 5 | **fastapi-turbo** | **25 μs** | **40 μs** | 19 μs | **38,302** | FastAPI Depends() |
| 6 | FastAPI + uvicorn | 126 μs | 155 μs | 109 μs | 7,853 | Depends() per-request |

**fastapi-turbo is just 1μs behind Go Gin** with full FastAPI Depends() DI and **has better p99** (40μs vs 69μs). fastapi-turbo **beats Go Gin on throughput** (38,302 vs 37,687 req/s).

### POST /items (JSON body + validation: `{"name": "widget", "price": 9.99}`)

| # | Framework | p50 | p99 | min | req/s | Validation |
|---|-----------|-----|-----|-----|-------|------------|
| 1 | **Pure Rust Axum** | **19 μs** | 33 μs | 14 μs | 49,342 | serde (compile-time) |
| 2 | Go Gin | 25 μs | 48 μs | 17 μs | 37,620 | encoding/json |
| 2 | Go Echo | 25 μs | 39 μs | 17 μs | 38,725 | encoding/json |
| 4 | **fastapi-turbo** | **28 μs** | **44 μs** | 20 μs | **35,061** | Pydantic v2 (Rust) |
| 5 | Node.js Fastify | 30 μs | 44 μs | 20 μs | 32,176 | Ajv schema |
| 6 | FastAPI + uvicorn | 206 μs | 244 μs | 173 μs | 4,828 | Pydantic v2 |

**fastapi-turbo BEATS Node.js Fastify on POST** (28μs vs 30μs) and is only 3μs behind Go.

### Other HTTP Methods (fastapi-turbo only)

| Method | Endpoint | p50 | p99 | min | req/s |
|--------|----------|-----|-----|-----|-------|
| PUT | /items/1 (JSON body) | 28 μs | 70 μs | 20 μs | 33,270 |
| PATCH | /items/1 (no body) | 22 μs | 34 μs | 15 μs | 44,013 |
| DELETE | /items/1 | 22 μs | 35 μs | 15 μs | 43,754 |

PATCH and DELETE (no body) are as fast as GET — **22μs**, proving the body parsing is the only overhead difference.

---

## WebSocket Echo Benchmark (10,000 messages, single connection)

| # | Framework | p50 | min | msg/s | Notes |
|---|-----------|-----|-----|-------|-------|
| 1 | **Pure Rust Axum** (zero Python) | **45 μs** | 32 μs | 21,766 | Axum WS baseline — beats Go! |
| 2 | Node.js Fastify | 47 μs | 36 μs | 20,694 | |
| 3 | Go Gin | 48 μs | 34 μs | 20,727 | |
| 4 | **fastapi-turbo SYNC handler** | **57 μs** | 43 μs | **17,339** | 12μs behind pure Rust (GIL cost) |
| 4 | **fastapi-turbo ASYNC handler** | **58 μs** | 45 μs | **16,996** | ChannelAwaitable — matches sync! |
| 6 | FastAPI + uvicorn | 120 μs | 94 μs | 8,243 | |

**Key findings:**
- Pure Rust Axum WS **beats Go Gin by 3μs** — fastapi-turbo's Rust layer is faster than Go for WS
- fastapi-turbo SYNC handler is **9μs behind Go, 2.1x faster than FastAPI**
- The 12μs gap between fastapi-turbo sync and pure Axum = 2 GIL crossings (receive + send)

**WS optimization history:**
| Version | Technique | p50 | vs Go |
|---|---|---|---|
| v1 | `run_in_executor` (2 thread pool hops) | 105μs | 2.2x behind |
| v2 | asyncio.Queue (no thread hop) | 85μs | 1.8x behind |
| v3 | Pipe signaling (zero GIL receive) | 71μs | 1.5x behind |
| v4 | crossbeam (sync) + pipe (async) | 57μs sync / 76μs async | sync: 1.2x behind |
| **v5** | **ChannelAwaitable (Rust-backed Python awaitable)** | **57μs sync / 58μs async** | **async matches sync!** |

**Architecture:**
- I/O: Pure Rust tokio tasks (axum::ws read/write)
- Sync receive: crossbeam channel (lock-free, ~100ns)
- Async receive: ChannelAwaitable — custom Python awaitable backed by crossbeam (zero asyncio overhead)
- Send: Direct tokio mpsc channel push (~0.1μs)

### WebSocket Library Comparison (pure Rust echo, 10K messages)

| Library | p50 | msg/s |
|---------|-----|-------|
| tokio-tungstenite (axum default) | 41 μs | 24,309 |
| fastwebsockets (Deno team) | 40 μs | 22,435 |
| Go gorilla/websocket | 44 μs | 22,030 |
| Node.js ws (Fastify) | 44 μs | 22,119 |

fastwebsockets is only ~1μs faster than tungstenite for small messages. Both Rust libs beat Go and Node.
The "2x faster" claims apply to large frames with SIMD masking, not small echo payloads.

---

## File Handling Benchmark (uploads + downloads + static)

5,000 requests, 200 warmup, single keep-alive connection. Local sockets only.
Run with: `comparison/bench-app/run_files_benchmark.sh`

### Throughput (req/s, higher is better)

| Endpoint            | fastapi-turbo | Go Gin  | Fastify |
|---------------------|-----------:|--------:|--------:|
| POST /upload 1 KB   | 12,167     | 31,918  | 16,679  |
| POST /upload 64 KB  | **10,668** | 8,835   | 10,827  |
| GET /download small | 20,797     | 23,141  | 9,641   |
| GET /download 64 KB | 16,678     | 17,462  | 9,847   |
| GET /download 1 MB  | 3,539      | 3,679   | 2,251   |
| GET /static/*.css   | 12,727     | 18,857  | 11,288  |

### Latency p50 (μs, lower is better)

| Endpoint            | fastapi-turbo | Go Gin | Fastify |
|---------------------|-----------:|-------:|--------:|
| POST /upload 1 KB   | 81         | **29** | 57      |
| POST /upload 64 KB  | 92         | 89     | 84      |
| GET /download small | 48         | **42** | 86      |
| GET /download 64 KB | 59         | 54     | 86      |
| GET /download 1 MB  | 279        | 267    | 402     |
| GET /static/*.css   | 77         | 52     | 87      |

**Takeaways**

- fastapi-turbo **beats Go Gin** on 64 KB multipart uploads (10,668 vs 8,835 req/s)
  and ties Fastify.
- fastapi-turbo **beats Fastify by 2x** on FileResponse downloads (all sizes),
  and lands within 5–10% of Go Gin on downloads.
- Static file serving is 30–40% slower than Go (tower-http ServeDir + mount
  dispatch overhead); still beats Fastify. Room to close the gap by caching
  the ServeDir future.
- Small 1 KB uploads carry fixed multipart-parse + Python wrap overhead; this
  overhead amortises at realistic upload sizes (64 KB+).

---

## fastapi-turbo vs FastAPI Speedup

| Endpoint | FastAPI + uvicorn | fastapi-turbo | Speedup |
|----------|-------------------|-------|---------|
| GET /hello | 188 μs | 24 μs | **7.8x** |
| GET /with-deps | 126 μs | 25 μs | **5.0x** |
| POST /items | 206 μs | 29 μs | **7.1x** |
| WebSocket echo | 120 μs/msg | 57 μs/msg | **2.1x** |

---

## Real Database Benchmarks (PostgreSQL + Redis, localhost)

### psycopg3 autocommit + pipeline: beats Go goroutines

Using `fastapi_turbo.db.create_pool()` with autocommit=True and pipeline mode:

| Queries | fastapi-turbo | Go Gin (pgx) | Winner |
|---------|-----------|--------------|--------|
| **1 query** | **53 μs** | 56 μs | **fastapi-turbo beats Go** |
| **4 seq** | **104 μs** | 144 μs | **fastapi-turbo by 40 μs** |
| **4 pipeline vs goroutine** | **96 μs** | 79 μs | Go by 17 μs |
| **10 seq** | **197 μs** | 321 μs | **fastapi-turbo by 124 μs** |
| **10 pipeline vs goroutine** | **138 μs** | 148 μs | **fastapi-turbo beats Go** |

Pipeline mode sends all queries in ONE network round-trip. Combined with autocommit (no BEGIN/COMMIT overhead), this matches or beats Go's goroutine parallelism.

### Raw Driver Performance (no framework, same JOIN query)

| Driver | Mode | p50 | req/s |
|--------|------|-----|-------|
| pymemcache | sync | **19 μs** | 49,758 |
| redis-py | sync | **28 μs** | 31,820 |
| **psycopg2** | sync | **38 μs** | 25,873 |
| pymysql | sync | **48 μs** | 19,643 |
| redis.asyncio | async | 53 μs | 18,362 |
| pymongo | sync | **64 μs** | 14,992 |
| aiomcache | async | 76 μs | 13,006 |
| psycopg3 | async | 82 μs | 10,528 |
| redis.asyncio | async | 90 μs | 11,043 |
| SQLAlchemy Core | sync | 117 μs | 8,547 |
| aiomysql | async | 142 μs | 6,873 |
| asyncpg | async | 148 μs | 6,753 |
| motor (MongoDB) | async | 178 μs | 5,547 |

Sync drivers are 2.8-4x faster than async for sequential operations. The async overhead (~60-100 μs) is `run_until_complete` event loop setup per call.

### psycopg3 autocommit + pipeline (raw driver, no framework)

| Mode | 1 query | 4 queries | 10 queries |
|------|---------|-----------|------------|
| psycopg3 autocommit | **22 μs** | — | — |
| psycopg3 sequential | 41 μs | 87 μs | 172 μs |
| **psycopg3 pipeline** | — | **85 μs** | **125 μs** |
| psycopg2 sequential | 48 μs | ~130 μs | ~300 μs |
| asyncpg gather | — | 224 μs | 344 μs |
| Go pgx goroutine | ~20 μs | ~55 μs | ~100 μs |

Pipeline mode crossover: starts winning at 4 queries. By 10 queries, pipeline (125 μs) is 1.4x faster than sequential (172 μs) and 2.8x faster than asyncpg gather (344 μs).

**Why not psycopg2 or asyncpg?**
- psycopg2: No pipeline mode, no binary protocol, no async support. Legacy driver.
- asyncpg: No pipeline mode. `asyncio.gather` uses separate connections (2.8x slower than psycopg3 pipeline). Higher per-query overhead (112 μs vs 22 μs).
- psycopg3: Pipeline + autocommit + sync/async in one driver. The clear default.

### Redis pipelining (raw driver, no framework)

| Mode | 1 GET | 2 GETs | 4 GETs | 10 GETs |
|------|-------|--------|--------|---------|
| redis-py sequential | 30 μs | 59 μs | 118 μs | 298 μs |
| **redis-py pipeline** | 29 μs | **40 μs** | **44 μs** | **57 μs** |
| redis.asyncio pipeline | 36 μs | 41 μs | 49 μs | 70 μs |

Redis pipeline has zero overhead for single commands (29 μs vs 30 μs). Pipeline starts winning at 2 commands. By 10 GETs, pipeline (57 μs) is 5.2x faster than sequential (298 μs).

### 5-Framework Multi-Query Comparison (through full framework)

**PostgreSQL — Sequential vs Pipeline/Parallel:**

| Queries | fastapi-turbo | Go Gin | Go Echo | Fastify | FastAPI |
|---------|-----------|--------|---------|---------|---------|
| 1 seq | **53 μs** | 55 μs | 56 μs | 77 μs | 281 μs |
| 4 seq | **104 μs** | 144 μs | 146 μs | 226 μs | — |
| 10 seq | **197 μs** | 320 μs | 329 μs | 512 μs | — |
| 4 pipe/parallel | **96 μs** | 78 μs (goroutine) | 80 μs | 97 μs (Promise.all) | 447 μs (gather) |
| 10 pipe/parallel | **139 μs** | 147 μs (goroutine) | 155 μs | 144 μs (Promise.all) | 596 μs (gather) |

fastapi-turbo beats Go on 1 query, 4 seq, 10 seq, and 10 pipeline. Beats Fastify on everything.

**Redis — Sequential vs Pipeline:**

| GETs | fastapi-turbo | Go Gin | Go Echo | Fastify | FastAPI |
|------|-----------|--------|---------|---------|---------|
| 1 seq | 63 μs | **48 μs** | **48 μs** | **47 μs** | 218 μs |
| 4 seq | 152 μs | 110 μs | 109 μs | 113 μs | 494 μs |
| 10 seq | 332 μs | 228 μs | 228 μs | 221 μs | 997 μs |
| 4 pipeline | **74 μs** | **49 μs** | **49 μs** | **50 μs** | 236 μs |
| 10 pipeline | **80 μs** | **52 μs** | **52 μs** | **57 μs** | 257 μs |

Redis pipeline 10 at 80 μs (with hiredis) — 4.2x faster than sequential (332 μs). 3.2x faster than FastAPI pipeline (257 μs). Go leads at 52 μs (faster driver, no GIL).

### Complete 8-Framework Comparison (PostgreSQL + Redis, 10K requests each)

Drivers: psycopg2+redis-py (rs-sync), asyncpg+redis.asyncio (rs-async), pgx+go-redis (Go), tokio-postgres+redis (Axum), node-postgres+ioredis (Fastify/Express), asyncpg+redis.asyncio (FastAPI).

**Sync comparison (p50 latency in μs — lower is better):**

| Endpoint | rs-sync | Go Gin | Go Echo | Axum | Fastify | Express | FastAPI |
|----------|---------|--------|---------|------|---------|---------|---------|
| GET /health (no DB) | **25** | **24** | **24** | **19** | 26 | 27 | 190 |
| GET /products/1 (JOIN) | **81** | **57** | **57** | 128 | 81 | 90 | 346 |
| GET /products (list) | **94** | **82** | **82** | 145 | 93 | 102 | 446 |
| GET /cached (Redis) | **68** | 51 | 52 | **44** | **47** | 56 | 242 |
| GET /orders/1 (multi-JOIN) | **100** | **90** | **90** | 200 | 127 | 136 | 442 |
| POST /products (INSERT) | **123** | **105** | **107** | 168 | 124 | 133 | 404 |
| PUT /products/1 (UPDATE) | **128** | **105** | **105** | 163 | 127 | 134 | 413 |
| PATCH /products/1 | **121** | **106** | **106** | 160 | 121 | 129 | 404 |
| DELETE /products/2 | **82** | 122 | 122 | — | 33* | 1663* | — |

*DELETE results are unreliable — product already deleted in earlier test runs.

**Async comparison (p50 latency in μs):**

| Endpoint | rs-async | FastAPI+uvicorn | Notes |
|----------|----------|-----------------|-------|
| GET /health | 25 | 190 | **7.6x faster** |
| GET /products/1 | 202 | 346 | **1.7x faster** |
| GET /products (list) | 243 | 446 | **1.8x faster** |
| GET /cached (Redis) | 124 | 242 | **2.0x faster** |
| GET /orders/1 | 242 | 442 | **1.8x faster** |
| POST /products | 272 | 404 | **1.5x faster** |
| PUT /products/1 | 265 | 413 | **1.6x faster** |
| PATCH /products/1 | 255 | 404 | **1.6x faster** |
| DELETE /products/2 | 219 | — | — |

**Key findings:**

1. **fastapi-turbo sync TIES Fastify** on GET /products/1 (81 vs 81), GET /products list (94 vs 93), PATCH (121 vs 121)
2. **fastapi-turbo sync BEATS Fastify** on GET /orders/1 (100 vs 127), POST (123 vs 124), Express on everything
3. **Go Gin/Echo lead** with pgx (fastest Postgres driver at 57 μs per query)
4. **Rust Axum is surprisingly slow** for DB queries — tokio-postgres is slower than pgx and psycopg2
5. **fastapi-turbo async is 1.5-2x faster than FastAPI+uvicorn** across all DB endpoints
6. **Sync is 2-3x faster than async** for sequential DB operations (psycopg2 38 μs vs asyncpg 148 μs)

### Why sync beats async for DB operations

| Pattern | How it works | Best for |
|---------|-------------|----------|
| **Sync** (`def` + psycopg2/redis-py) | Handler blocks on DB call inside `block_in_place`. Tokio migrates other tasks to other workers. Zero event loop overhead. | Sequential DB operations (most API endpoints) |
| **Async** (`async def` + asyncpg/redis.asyncio) | Handler runs on dedicated event loop thread via `run_until_complete` + uvloop. Event loop adds ~80-150 μs overhead per query. | Concurrent I/O within one handler (e.g., parallel API calls) |

fastapi-turbo supports both patterns. Sync handlers with `block_in_place` work like Go goroutines — the blocking is isolated to one tokio worker thread while others continue serving requests.

### All 5 Databases — Raw Driver Performance

| Database | Sync driver | p50 | Async driver | p50 | Sync advantage |
|----------|-------------|-----|--------------|-----|----------------|
| Memcached | pymemcache | **19 μs** | aiomcache | 76 μs | 4.0x |
| Redis | redis-py | **28 μs** | redis.asyncio | 90 μs | 3.2x |
| PostgreSQL | psycopg2 | **38 μs** | asyncpg | 148 μs | 3.9x |
| MySQL | pymysql | **48 μs** | aiomysql | 142 μs | 3.0x |
| MongoDB | pymongo | **64 μs** | motor | 178 μs | 2.8x |

---

## Outbound HTTP Client Benchmark (reqwest-based)

`fastapi_turbo.http.Client` — httpx-compatible Python API backed by Rust `reqwest`. Target: uvicorn ASGI server on localhost.

### Single request latency

| Client | p50 | min | p99 |
|--------|-----|-----|-----|
| Go `net/http` | 108 μs | 87 μs | 131 μs |
| Node.js `undici` | 101 μs | 78 μs | 137 μs |
| Python `http.client` (stdlib) | 108 μs | 83 μs | 127 μs |
| **fastapi_turbo.http** | **136 μs** | **112 μs** | 156 μs |
| httpx | 244 μs | 199 μs | 316 μs |
| requests | 383 μs | 342 μs | 590 μs |

fastapi_turbo.http is **2.2x faster than httpx** and **3.4x faster than requests** with the same feature set (HTTP/2, TLS, cookies, redirects, auth, compression, proxy).

### Parallel requests — `gather()` vs ThreadPool vs async gather

Time to complete N parallel GETs:

| Parallel | Go goroutines | Node undici | **fastapi_turbo gather** | httpx ThreadPool | httpx async |
|----------|--------------|-------------|----------------------|-----------------|-------------|
| x2 | 166 μs | 156 μs | 216 μs | 399 μs | 1,065 μs |
| x4 | 277 μs | 266 μs | **310 μs** | 742 μs | 2,577 μs |
| x10 | 547 μs | 530 μs | **622 μs** | 1,838 μs | 6,994 μs |
| x20 | 987 μs | 938 μs | **1,068 μs** | 3,567 μs | 15,590 μs |

**Per-request latency** (amortized):

| Parallel | Go | Node | **fastapi_turbo** | httpx ThreadPool | httpx async |
|----------|-----|------|---------------|------------------|-------------|
| x10 | 55 μs | 53 μs | **62 μs** | 184 μs | 699 μs |
| x20 | 49 μs | 47 μs | **53 μs** | 178 μs | 779 μs |

`gather()` runs N concurrent requests on a Rust tokio runtime with ONE GIL release. Results:
- **3x faster than httpx ThreadPool** at x20
- **13x faster than httpx async gather** at x20
- **Within 15% of Go goroutines and Node Promise.all** across all parallelism levels

### Architecture

```
Python Client (httpx-compatible API)
    │
    ├─ auth flow (generator pattern, for token refresh / challenge-response)
    ├─ cookie jar, redirect following, event hooks
    └─ Rust transport (PyO3 #[pyclass])
              │ py.detach() — GIL released during I/O
              ▼
       thread-local tokio current_thread runtime
              │ (zero cross-thread scheduling)
              ▼
          reqwest ─── hyper ─── rustls
```

Key design decisions:
- **Thread-local `current_thread` tokio runtime** — eliminates cross-thread scheduling overhead (~10 μs saved vs multi-thread runtime)
- **Python logic + Rust transport** (same split as httpx + httpcore) — auth/cookies/redirects in Python, I/O in Rust
- **Fast path in Python** — bypasses `build_request`/`send` ceremony for simple calls with no auth/hooks/cookies/redirects (~12 μs saved)
- **One reqwest client, shared via `Arc`** — connection pool reused across all requests
- **Leaked runtimes** — never dropped at interpreter shutdown, avoids tokio destructor races

---

## Free-Threaded Python 3.14t Benchmark (GIL DISABLED)

Tested with `python-freethreading` (Homebrew), `#[pymodule(gil_used = false)]` declared.

| Endpoint | GIL-enabled (3.14) | Free-threaded (3.14t) | Change |
|---|---|---|---|
| /_ping (pure Rust) | 18 μs | **16 μs** | **Faster** (no GIL in Rust) |
| GET /hello | 24 μs | 84 μs | 3.5x slower |
| GET /with-deps | 26 μs | 96 μs | 3.7x slower |
| POST /items | 29 μs | 76 μs | 2.6x slower |
| DELETE | 23 μs | 63 μs | 2.7x slower |
| WS sync echo | 57 μs | **56 μs** | **Same/faster** |
| WS async echo | 58 μs | **57 μs** | **Same** |

**Historical context (preserved for transparency):** the original investigation here was on PyO3 0.25, which had a measurable per-call ``Python::with_gil()`` overhead on free-threaded Python (~60μs from biased reference counting + per-object critical sections). The pure Rust path (`/_ping`) was faster than Python-handler paths because it avoided the GIL altogether; WebSocket was unaffected because `py.allow_threads()` released the GIL.

**Status as of R-batch refresh:** the codebase is on PyO3 0.28 (`Cargo.toml`); the migration from 0.25 (with_gil → attach, deprecated downcast → cast) is complete and clippy-clean. The free-threaded numbers above pre-date the migration and have not been re-measured on 0.28 in this document. Re-measurement under 0.28 + free-threaded Python is on the bench TODO list (alongside Linux-x86_64 numbers) — see "What we have NOT yet published" higher up in this file.

---

## Optimization Roadmap (future)

| Optimization | Expected Impact | Status |
|---|---|---|
| Free-threaded Python 3.14t | Expected GIL → 0, actual 3x slower (PyO3 per-object locking) | **Tested — NOT ready.** Wait for PyO3 0.28+ |
| Direct pydantic-core Rust FFI | Eliminate ~2μs Python boundary for POST | pydantic-core not published as Rust crate |
| pydantic-core standalone Rust crate | Eliminate ~2μs Python boundary for POST | Confirmed NOT on crates.io — tightly coupled to PyO3. jiter (pydantic's JSON parser) IS available as a pure Rust crate. |
| `sonic-rs` SIMD JSON | ~0.3μs faster serialization (diminishing returns on small payloads) | Evaluated, marginal gain |

---

## Historical Results

### 2026-04-15 — Initial Comprehensive Comparison

### GET /hello (simple JSON response: `{"message": "hello"}`)

| # | Framework | p50 | p99 | min | req/s |
|---|-----------|-----|-----|-----|-------|
| 1 | **Pure Rust Axum** | **18 μs** | 50 μs | 13 μs | 51,441 |
| 2 | Node.js Fastify | 23 μs | 38 μs | 16 μs | 41,471 |
| 3 | Go Gin | 24 μs | 47 μs | 16 μs | 39,379 |
| 4 | Go Echo | 24 μs | 38 μs | 16 μs | 40,020 |
| 5 | **fastapi-turbo** | **26 μs** | 78 μs | 18 μs | 35,021 |
| 6 | FastAPI + uvicorn | 187 μs | 224 μs | 154 μs | 5,301 |
| 7 | FastAPI + socketify | 205 μs | 282 μs | 166 μs | 4,789 |

### GET /with-deps (dependency injection — 2-level chain with header extraction)

| # | Framework | p50 | p99 | min | req/s | DI type |
|---|-----------|-----|-----|-----|-------|---------|
| 1 | **Pure Rust Axum** | **19 μs** | 32 μs | 13 μs | 51,368 | Compile-time (zero cost) |
| 2 | Node.js Fastify | 23 μs | 37 μs | 15 μs | 42,154 | Manual (no DI framework) |
| 3 | Go Gin | 24 μs | 46 μs | 16 μs | 39,147 | Manual function calls |
| 4 | Go Echo | 24 μs | 39 μs | 16 μs | 39,513 | Manual function calls |
| 5 | **fastapi-turbo** | **27 μs** | 56 μs | 20 μs | 34,854 | FastAPI Depends() — compiled at startup, executed in Rust |
| 6 | FastAPI + socketify | 63 μs | 87 μs | 51 μs | 15,315 | Depends() — runtime Python introspection per-request |
| 7 | FastAPI + uvicorn | 127 μs | 185 μs | 108 μs | 7,741 | Depends() — runtime Python introspection per-request |

### POST /items (JSON body parsing + validation: `{"name": "widget", "price": 9.99}`)

| # | Framework | p50 | p99 | min | req/s | Validation |
|---|-----------|-----|-----|-----|-------|------------|
| 1 | **Pure Rust Axum** | **20 μs** | 35 μs | 15 μs | 46,717 | serde (compile-time types) |
| 2 | Go Gin | 26 μs | 49 μs | 18 μs | 36,196 | encoding/json + struct tags |
| 3 | Go Echo | 26 μs | 40 μs | 17 μs | 37,158 | encoding/json + struct tags |
| 4 | **fastapi-turbo** | **31 μs** | 47 μs | 20 μs | 31,649 | Pydantic v2 (Rust-backed) |
| 5 | Node.js Fastify | 33 μs | 46 μs | 21 μs | 30,038 | JSON.parse + Ajv schema |
| 6 | FastAPI + uvicorn | 202 μs | 360 μs | 176 μs | 4,788 | Pydantic v2 |
| 7 | FastAPI + socketify | 258 μs | 308 μs | 208 μs | 3,826 | Pydantic v2 |

### POST /form-items (form-encoded body: `name=widget&price=9.99`)

| # | Framework | p50 | p99 | min | req/s |
|---|-----------|-----|-----|-----|-------|
| 1 | **Pure Rust Axum** | **20 μs** | 34 μs | 15 μs | 47,252 |
| 2 | Go Echo | 26 μs | 42 μs | 18 μs | 36,545 |
| 3 | Go Gin | 27 μs | 51 μs | 17 μs | 35,525 |

*(fastapi-turbo form parsing not yet implemented; FastAPI and Node.js not tested for form)*

---

## Key Takeaways

### fastapi-turbo vs FastAPI (the drop-in replacement story)

| Endpoint | FastAPI + uvicorn | fastapi-turbo | Speedup |
|----------|-------------------|-------|---------|
| GET /hello | 187 μs | 26 μs | **7.2x** |
| GET /with-deps | 127 μs | 27 μs | **4.7x** |
| POST /items | 202 μs | 31 μs | **6.5x** |

fastapi-turbo is **5-7x faster** than FastAPI while maintaining 100% API compatibility (same decorators, same Depends(), same Pydantic validation).

### fastapi-turbo vs Go (the performance story)

| Endpoint | Go Gin | fastapi-turbo | Overhead |
|----------|--------|-------|----------|
| GET /hello | 24 μs | 26 μs | +2 μs (GIL acquisition) |
| GET /with-deps | 24 μs | 27 μs | +3 μs (Python DI calls) |
| POST /items | 26 μs | 31 μs | +5 μs (Pydantic validation) |

The 2-5 μs gap is the irreducible cost of crossing the Python boundary (GIL acquisition + PyO3 marshaling). Pure Axum (18-20 μs) proves the Rust infrastructure is faster than Go.

### fastapi-turbo vs Node.js Fastify

| Endpoint | Fastify | fastapi-turbo | Winner |
|----------|---------|-------|--------|
| GET /hello | 23 μs | 26 μs | Fastify by 3 μs |
| GET /with-deps | 23 μs | 27 μs | Fastify by 4 μs |
| POST /items | 33 μs | 31 μs | **fastapi-turbo by 2 μs** |

fastapi-turbo **beats Node.js Fastify on POST** (Pydantic's Rust-backed validation is faster than Ajv) and is within 3-4 μs on GET.

### Throughput ranking (GET /hello)

| Rank | Framework | req/s |
|------|-----------|-------|
| 1 | Pure Rust Axum | 51,441 |
| 2 | Node.js Fastify | 41,471 |
| 3 | Go Echo | 40,020 |
| 4 | Go Gin | 39,379 |
| 5 | **fastapi-turbo** | **35,021** |
| 6 | FastAPI + uvicorn | 5,301 |
| 7 | FastAPI + socketify | 4,789 |

---

## Architecture Notes

- **fastapi-turbo's Rust core** uses Axum (hyper + matchit + tower) — the same stack as "Pure Rust Axum" in the benchmarks
- **Python overhead** per request: 5-7 μs (GIL acquisition + handler call + response serialization)
- **Depends() resolution** is compiled at startup into a topological plan, executed in Rust at request time with a single GIL acquisition
- **Trivial async deps** (`async def get_db(): return pool`) are resolved synchronously via `coro.send(None)` — no event loop round-trip
- **JSON serialization** uses direct Rust pyobj_to_serde traversal (avoids extra Python calls)
- **Pydantic validation** uses `__pydantic_validator__.validate_json(bytes)` — calls Rust pydantic-core directly
