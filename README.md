# fastapi-turbo

Drop-in replacement for FastAPI, powered by Rust + Axum. **Hello-world throughput on macOS loopback at `c=1` only** is competitive with Fastify (Node.js), Go Gin, and Go Echo — at higher concurrencies (`c=32+`) Go's per-core goroutine model pulls ahead. CRUD-style workloads (Pydantic body validation + JSON encode + ORM hops) land at roughly 65–85% of Go on the same hardware. See [benchmarks.md](benchmarks.md) and [benchmarks/latest_bench.md](benchmarks/latest_bench.md) for the full breakdown across `c=1 / 32 / 256` and CRUD/echo/redis/sql/sse workloads, and the documented caveats (Linux-x86_64 numbers pending, `wrk`/`bombardier` cross-checks pending).

```python
# Change one import — everything else stays the same
from fastapi_turbo import FastAPI, Depends, Query, Header
from pydantic import BaseModel

app = FastAPI()

class Item(BaseModel):
    name: str
    price: float

async def get_db():
    return db_pool

@app.get("/items/{item_id}")
async def get_item(item_id: int, db=Depends(get_db)):
    return await db.fetch_one("SELECT * FROM items WHERE id=$1", item_id)

@app.post("/items")
def create_item(item: Item):
    return {"name": item.name, "price": item.price, "created": True}

app.run()
```

## Performance

### Methodology

All numbers below come from the custom `fastapi-turbo-bench` Rust HTTP
client. To keep the comparison honest, the published table uses the
following configuration — **re-run locally before relying on any
claim here**:

- **Load shape**: `--connections 1`, 20K requests, keep-alive. This
  measures per-request overhead in a single-flight flight pattern.
  Real traffic is concurrent; see the "Concurrent" table below for
  multi-connection numbers (these change the picture).
- **Hardware**: Apple Silicon M-series, local loopback. Linux numbers
  differ — epoll / kqueue overhead, NUMA effects, and distro scheduler
  defaults all shift the results. **Linux x86_64 numbers are not yet published** — see [benchmarks.md](benchmarks.md) "What we have NOT yet published" for the gap list. Until they are, treat the numbers below as macOS-loopback per-request overhead — useful for relative comparison on this hardware, not as production-rollout sizing.
- **Cross-check**: rows in the single-connection table have been
  re-run with `oha -c 1` and the deltas noted when they disagree
  by more than 2 μs. `wrk` and `bombardier` cross-checks are still
  TODO — see [benchmarks.md](benchmarks.md) "What we have NOT yet
  published" for the gap list. Until those run, treat `oha` as the
  single independent confirmation, not a multi-tool consensus.

### HTTP (p50 latency, single connection, lower is better)

| Endpoint | FastAPI | **fastapi-turbo** | Go Gin | Go Echo | Fastify | Speedup vs FastAPI |
|----------|---------|---------------|--------|---------|---------|-------------------|
| GET /hello | 188 us | **24 us** | 24 us | 24 us | 24 us | **7.8x** |
| GET /with-deps (2-level DI) | 126 us | **26 us** | 24 us | 24 us | 23 us | **4.8x** |
| POST /items (Pydantic) | 206 us | **29 us** | 26 us | 26 us | 30 us | **7.1x** |
| DELETE | — | **23 us** | — | — | — | — |
| PATCH | — | **23 us** | — | — | — | — |

These are **single-connection loopback** numbers — good for
measuring per-request overhead, not a production workload. For
concurrent load (c=32, c=256) and Linux measurements, see
[benchmarks.md](benchmarks.md). The ordering against Go/Fastify
shifts at higher concurrency because our worker loop model is
different from their per-core thread pools.

### WebSocket (p50 latency per echo round-trip, 10K messages)

| Framework | Sync handler | Async handler | msg/s |
|-----------|-------------|--------------|-------|
| Pure Rust Axum (zero Python) | 45 us | — | 22,000 |
| Go Gin | 48 us | — | 20,700 |
| Fastify | 47 us | — | 20,700 |
| **fastapi-turbo** | **57 us** | **58 us** | **17,000** |
| FastAPI + uvicorn | 120 us | 120 us | 8,200 |

fastapi-turbo async WebSocket matches sync (58 us vs 57 us) thanks to `ChannelAwaitable` — a custom Python awaitable backed by a Rust lock-free channel that bypasses asyncio scheduling entirely. **2.1x faster than FastAPI**.

## How It Works

```
Your Python code (unchanged FastAPI handlers)
        |
        v
fastapi-turbo Python layer (decorators, Depends, Pydantic)
        | inspect.signature() at startup -> compile dependency graphs
        v
PyO3 boundary (1 GIL acquisition per request)
        |
        v
Rust core: Axum HTTP + matchit routing + Tower middleware
        | pre-compiled dependency resolution
        | direct PyDict->JSON response writer
        v
hyper HTTP server (same as pure Rust performance)
```

The key innovation: FastAPI's `Depends()` resolution is compiled into a topological execution plan at startup and executed in Rust. This reduces per-request DI overhead from ~297 us (FastAPI) to ~7 us.

## Install

```bash
pip install fastapi-turbo
```

Requires Python 3.10+. Pre-built wheels for Linux (x86_64, aarch64), macOS (x86_64, ARM), Windows.

## Zero-effort migration from FastAPI

fastapi-turbo intercepts `import fastapi` and `import starlette` at the Python module level, so your existing FastAPI code works without any changes:

```python
import fastapi_turbo  # Activate once — all subsequent FastAPI imports redirect here

# Your existing code, unchanged:
from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import JSONResponse
from starlette.requests import Request
from starlette.middleware.cors import CORSMiddleware
```

Every `from fastapi ...` and `from starlette ...` import automatically resolves to the fastapi-turbo equivalent. No find-and-replace needed.

To disable this and use both FastAPI and fastapi-turbo side by side, set `FASTAPI_TURBO_NO_SHIM=1`.

### What's supported

- All HTTP methods (GET, POST, PUT, DELETE, PATCH, OPTIONS, HEAD)
- Path, Query, Header, Cookie, Body, Form, File parameters
- `typing.Annotated` parameter pattern
- `Depends()` with nested chains, caching, async deps, `dependency_overrides`
- Pydantic v2 body validation (via pydantic-core's Rust backend)
- `response_model` — output filtering, aliases, `model_validate(obj)`
- `status_code`, `tags`, `summary`, `description` on routes
- WebSocket with `send_text/bytes/json`, `receive_text/bytes/json`; sync + async parity
- OpenAPI 3.1 auto-generation, Swagger UI (`/docs`), ReDoc (`/redoc`)
- CORS, GZip, TrustedHost, HTTPSRedirect (Tower-backed, ~0.3 us per request)
- ASGI middleware: `BaseHTTPMiddleware`, raw `(scope, receive, send)` — Sentry, Prometheus, SessionMiddleware work
- StreamingResponse (sync + async generators), `EventSourceResponse` (SSE)
- `FileResponse` with `Range:` → `206 Partial Content`, `StaticFiles`, `Jinja2Templates`
- `app.mount("/sub", sub_app)` for sub-FastAPI / StaticFiles mounting
- Lifespan (`lifespan=` context manager) + `on_event("startup"|"shutdown")`
- Yield dependencies: teardown runs after middleware unwinds
- BackgroundTasks (single-task `background=` and multi-task `BackgroundTasks`)
- TestClient (httpx, real HTTP) — includes `websocket_connect(...)`
- Security: OAuth2PasswordBearer, HTTPBearer, HTTPBasic, APIKeyHeader / Query / Cookie
- `jsonable_encoder`, status code constants, `run_in_threadpool`, `redirect_slashes`

### Known limitations

- HTTP/3 + QUIC not yet exposed (Axum stack is HTTP/1.1 + HTTP/2)
- Free-threaded Python (3.13t/3.14t) works but hasn't been perf-tuned
- `AsyncClient(transport=ASGITransport(app=app))` dispatches fully in-process (no loopback socket). Verified parity with upstream FastAPI across 404/405/HEAD/OPTIONS, `Header(...)`/`Cookie(...)`/`Query(...)`, path param type coercion → 422, invalid Pydantic body → 422, nested `Depends`/`Security` with inner params, `response_model` validation failures, and custom `@app.exception_handler` routing.

### ⚠️ Public-internet checklist

- **Always set `max_request_size` for public servers.** fastapi-turbo matches
  Starlette's default (no framework-imposed body cap). A bare `FastAPI()`
  with no limit lets a client stream an arbitrarily large body and consume
  worker memory — a trivial DoS footgun. Set it:

  ```python
  from fastapi_turbo import FastAPI

  app = FastAPI(max_request_size=10 * 1024 * 1024)  # 10 MiB
  ```

  Oversized requests respond `413 Payload Too Large` via the Tower layer.
- Run behind a reverse proxy (nginx / Caddy / an L7 LB) that terminates
  TLS and enforces a connection-level byte ceiling as a second line of
  defense.
- Set `FASTAPI_TURBO_WORKER_TIMEOUT` (or `FastAPI(worker_timeout=...)`)
  so a single runaway async handler can't pin the shared worker loop.

## Database: Use psycopg3 (not psycopg2 or asyncpg)

fastapi-turbo is fastest with **psycopg3** — it supports autocommit mode (eliminates transaction overhead) and pipeline mode (sends multiple queries in one network round-trip). psycopg2 and asyncpg lack both features.

### Quick start

```bash
pip install "psycopg[binary,pool]"
```

```python
from fastapi_turbo import FastAPI
from fastapi_turbo.db import create_pool

app = FastAPI()
pool = create_pool("dbname=mydb user=myuser")  # autocommit=True by default

# Single query — 53us on macOS loopback at c=1; see benchmarks.md for cross-concurrency numbers
@app.get("/users/{user_id}")
def get_user(user_id: int):
    with pool.connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=%s", (user_id,)).fetchone()
    return dict(row)

# Multiple queries — use pipeline mode (138us for 10 queries on macOS loopback at c=1; see benchmarks.md for cross-concurrency numbers)
@app.get("/dashboard")
def dashboard(user_id: int):
    with pool.connection() as conn:
        with conn.pipeline():
            user = conn.execute("SELECT * FROM users WHERE id=%s", (user_id,))
            orders = conn.execute("SELECT * FROM orders WHERE uid=%s", (user_id,))
            stats = conn.execute("SELECT COUNT(*) FROM orders WHERE uid=%s", (user_id,))
            return {
                "user": dict(user.fetchone()),
                "orders": [dict(r) for r in orders.fetchall()],
                "stats": dict(stats.fetchone()),
            }
```

### Why psycopg3 and not psycopg2 or asyncpg?

| Feature | psycopg3 | psycopg2 | asyncpg |
|---------|----------|----------|---------|
| Pipeline mode (batch queries) | Yes | No | No |
| Autocommit (skip BEGIN/COMMIT) | Yes | Limited | Yes |
| Binary protocol | Yes | No | Yes |
| Sync + async in same driver | Yes | Sync only | Async only |
| **1 query latency** | **22us** | 48us | 112us |
| **10 queries (pipeline)** | **102us** | ~300us (seq) | 344us (gather) |

### Performance: fastapi-turbo vs Go Gin (through full framework, macOS loopback at c=1 only)

> **Note:** these are single-connection (`c=1`) macOS-loopback numbers and do NOT generalise to higher concurrency or Linux production. At `c=32+` Go's per-core goroutine model pulls ahead on every workload type. See [benchmarks.md](benchmarks.md) for the concurrent-load and cross-tool cross-checks; treat the table below as a per-request-overhead snapshot, not a release headline.

| Queries | fastapi-turbo | Go Gin |
|---------|-----------|--------|
| 1 query (autocommit) | 53us | 56us |
| 4 queries (pipeline vs goroutine) | 96us | 79us |
| 10 queries (pipeline vs goroutine) | 138us | 148us |
| 4 queries (sequential) | 104us | 144us |
| 10 queries (sequential) | 197us | 321us |

### Autocommit and transactions

`create_pool()` enables autocommit by default — each query runs independently without transaction overhead. This is the fastest mode for read-heavy APIs.

For explicit transactions, disable autocommit:

```python
pool = create_pool("dbname=mydb", autocommit=False)

@app.post("/transfer")
def transfer(from_id: int, to_id: int, amount: float):
    with pool.connection() as conn:
        with conn.transaction():
            conn.execute("UPDATE accounts SET balance=balance-%s WHERE id=%s", (amount, from_id))
            conn.execute("UPDATE accounts SET balance=balance+%s WHERE id=%s", (amount, to_id))
    return {"status": "ok"}
```

### Redis

```bash
pip install "redis[hiredis]"
```

```python
from fastapi_turbo.db import create_redis

cache = create_redis()  # redis-py + hiredis, auto decode_responses=True

# Single command (63us through framework)
@app.get("/product/{product_id}")
def get_product(product_id: int):
    cached = cache.get(f"product:{product_id}")
    if cached:
        return json.loads(cached)
    # ... fetch from DB, cache it
    cache.set(f"product:{product_id}", json.dumps(data), ex=60)
    return data

# Multiple commands — use pipeline (91us for 10 GETs, 3.6x faster than sequential)
@app.get("/products/batch")
def get_batch():
    with cache.pipeline() as pipe:
        for i in range(10):
            pipe.get(f"product:{i}")
        results = pipe.execute()
    return {"products": [json.loads(r) for r in results if r]}
```

| Mode | 1 GET | 4 GETs | 10 GETs |
|------|-------|--------|---------|
| Sequential | 58us | 152us | 332us |
| **Pipeline + hiredis** | 58us | **70us** | **80us** |

Pipeline has zero overhead for single commands and is 4.2x faster at 10 commands. hiredis (C parser) is auto-detected when installed — adds 10-18% speed. `create_redis()` sets decode_responses=True by default.

### SQLAlchemy compatibility

`create_pool()` returns a standard psycopg3 `ConnectionPool`. SQLAlchemy works with psycopg3 via the `psycopg` driver:

```python
from sqlalchemy import create_engine, text

engine = create_engine("postgresql+psycopg://user@localhost/mydb")

@app.get("/users")
def list_users():
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT * FROM users")).fetchall()
        conn.commit()
    return [dict(r._mapping) for r in rows]
```

For maximum SQLAlchemy performance, use autocommit execution:

```python
with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
    rows = conn.execute(text("SELECT * FROM users")).fetchall()
```

### Performance tips

- **Use `def` handlers** (not `async def`) for DB-heavy endpoints — sync is 2-3x faster than async for sequential queries
- **Use psycopg3** (not psycopg2 or asyncpg) — pipeline + autocommit + fastest sync driver
- **Pipeline Postgres** when running 4+ queries: `with conn.pipeline(): results = [conn.execute(q) for q in queries]`
- **Pipeline Redis** when running 2+ commands: `with cache.pipeline() as p: [p.get(k) for k in keys]; p.execute()`
- **Use `create_pool()`** from `fastapi_turbo.db` — enables autocommit by default (saves 5μs per request)
- **Use `create_redis()`** from `fastapi_turbo.db` — enables hiredis + decode_responses by default
- **Install hiredis** for Redis: `pip install "redis[hiredis]"` — C response parser, 18% faster pipelines (auto-detected by redis-py)
- **Disable autocommit** only when you need transactions: `create_pool(dsn, autocommit=False)`

## HTTP Client

`fastapi_turbo.http.Client` is a drop-in replacement for `httpx.Client`, backed by Rust `reqwest`. Matches httpx's API exactly (same `Client`, `Response`, `Auth`, `Timeout`, `Limits`, event hooks, generator-based auth flow), but 2.2x faster on single requests and up to 3x faster on parallel fan-out.

```python
from fastapi_turbo.http import Client, BasicAuth, Timeout

client = Client(
    base_url="https://api.example.com",
    auth=("user", "pass"),
    timeout=Timeout(5.0, connect=10.0),
    http2=True,
    follow_redirects=True,
)

# Standard httpx-identical API
resp = client.get("/users/1")
data = resp.json()
resp.raise_for_status()

# gather() — N parallel requests in Rust with a single GIL release
# 3x faster than httpx ThreadPool, 13x faster than httpx async gather
responses = client.gather(["/users/1", "/users/2", "/users/3"])
```

### Features (matches httpx)

- All HTTP methods: `get`, `post`, `put`, `patch`, `delete`, `head`, `options`, `request`
- Built-in auth: `BasicAuth`, `DigestAuth`, `NetRCAuth`, bearer via headers
- Custom auth via generator-based `auth_flow` (httpx-compatible for token refresh on 401, challenge-response, etc.)
- `Timeout` with 4-way granularity (connect/read/write/pool)
- `Limits` for connection pool configuration
- Request/response `event_hooks`
- Automatic redirect following with cross-origin auth stripping
- Cookie jar, multipart upload, params, JSON body
- HTTP/2, TLS (rustls), gzip/brotli/deflate/zstd decompression
- HTTP and SOCKS proxy support
- `raise_for_status()`, `response.is_success`, `response.elapsed`, `response.history`

### Performance (single request, uvicorn localhost target)

| Client | p50 | Notes |
|--------|-----|-------|
| Go `net/http` | 108 μs | baseline |
| Node.js `undici` | 101 μs | Fastify's HTTP client |
| **fastapi_turbo.http** | **136 μs** | full httpx-compatible API |
| Python `http.client` stdlib | 108 μs | no features |
| httpx | 244 μs | |
| requests | 383 μs | |

Only 28 μs slower than Go despite crossing the Python boundary. **2.2x faster than httpx** with the same feature set.

### `gather()` — parallel requests, the killer feature

| Parallel calls | Go goroutines | Node Promise.all | **fastapi_turbo gather** | httpx ThreadPool | httpx async gather |
|---------------|---------------|------------------|----------------------|------------------|-------------------|
| x4 | 277 μs | 266 μs | **310 μs** | 742 μs | 2,577 μs |
| x10 | 547 μs | 530 μs | **622 μs** | 1,838 μs | 6,994 μs |
| x20 | 987 μs | 938 μs | **1,068 μs** | 3,567 μs | 15,590 μs |

`gather()` runs N concurrent requests on the tokio runtime with a **single GIL release** — no thread handoffs, no asyncio overhead. Within 15% of Go and Node at x10+.

## Development

```bash
# Build
pip install maturin
maturin develop --release

# Test
pytest tests/ -x -q

# Benchmark
python benchmarks/bench_hello.py
```

## Architecture

- **Rust core** (~8K lines): Axum 0.8, hyper, tokio, Tower, PyO3 0.28, crossbeam; HTTP, WebSocket, multipart, streaming, DB pool, HTTP client
- **Python layer** (~22K lines): FastAPI-compatible API, introspection, OpenAPI 3.1 generator, Starlette/FastAPI `sys.modules` compat shims
- **Tests** (~45K lines): 972 tests spanning HTTP, WebSocket, parity against real FastAPI on 16 parity apps, OpenAPI schema diffs, validation-error shape, SQLAlchemy × 3 drivers, Redis sync+async

See [CLAUDE.md](CLAUDE.md) for development guide, [benchmarks.md](benchmarks.md) for full benchmark data including Go Echo, Fastify, free-threaded Python, and WebSocket library comparisons, and [COMPATIBILITY.md](COMPATIBILITY.md) for a per-feature map of where fastapi_turbo sits against FastAPI 0.136.0 (Full / Partial / Not-implemented / Different-by-design).

## License

MIT
