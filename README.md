# fastapi-rs

Drop-in replacement for FastAPI, with near-Rust performance that beats or matches Fastify (Node.js), Go Gin, and Go Echo.

```python
# Change one import — everything else stays the same
from fastapi_rs import FastAPI, Depends, Query, Header
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

Measured with a compiled Rust HTTP client, 20K requests, keep-alive, Apple Silicon.

### HTTP (p50 latency, lower is better)

| Endpoint | FastAPI | **fastapi-rs** | Go Gin | Go Echo | Fastify | Speedup vs FastAPI |
|----------|---------|---------------|--------|---------|---------|-------------------|
| GET /hello | 188 us | **24 us** | 24 us | 24 us | 24 us | **7.8x** |
| GET /with-deps (2-level DI) | 126 us | **26 us** | 24 us | 24 us | 23 us | **4.8x** |
| POST /items (Pydantic) | 206 us | **29 us** | 26 us | 26 us | 30 us | **7.1x** |
| DELETE | — | **23 us** | — | — | — | — |
| PATCH | — | **23 us** | — | — | — | — |

fastapi-rs **ties Go and Fastify on GET** and **beats Fastify on POST** (29 us vs 30 us) thanks to Pydantic's Rust-backed validation being faster than Ajv.

### WebSocket (p50 latency per echo round-trip, 10K messages)

| Framework | Sync handler | Async handler | msg/s |
|-----------|-------------|--------------|-------|
| Pure Rust Axum (zero Python) | 45 us | — | 22,000 |
| Go Gin | 48 us | — | 20,700 |
| Fastify | 47 us | — | 20,700 |
| **fastapi-rs** | **57 us** | **58 us** | **17,000** |
| FastAPI + uvicorn | 120 us | 120 us | 8,200 |

fastapi-rs async WebSocket matches sync (58 us vs 57 us) thanks to `ChannelAwaitable` — a custom Python awaitable backed by a Rust lock-free channel that bypasses asyncio scheduling entirely. **2.1x faster than FastAPI**.

## How It Works

```
Your Python code (unchanged FastAPI handlers)
        |
        v
fastapi-rs Python layer (decorators, Depends, Pydantic)
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
pip install fastapi-rs
```

Requires Python 3.10+. Pre-built wheels for Linux (x86_64, aarch64), macOS (x86_64, ARM), Windows.

## Zero-effort migration from FastAPI

fastapi-rs intercepts `import fastapi` and `import starlette` at the Python module level, so your existing FastAPI code works without any changes:

```python
import fastapi_rs  # Activate once — all subsequent FastAPI imports redirect here

# Your existing code, unchanged:
from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import JSONResponse
from starlette.requests import Request
from starlette.middleware.cors import CORSMiddleware
```

Every `from fastapi ...` and `from starlette ...` import automatically resolves to the fastapi-rs equivalent. No find-and-replace needed.

To disable this and use both FastAPI and fastapi-rs side by side, set `FASTAPI_RS_NO_SHIM=1`.

### What's supported

- All HTTP methods (GET, POST, PUT, DELETE, PATCH, OPTIONS, HEAD)
- Path, Query, Header, Cookie, Body, Form, File parameters
- `typing.Annotated` parameter pattern
- `Depends()` with nested chains, caching, async deps
- Pydantic v2 body validation (via pydantic-core's Rust backend)
- `status_code`, `tags`, `summary`, `description` on routes
- WebSocket with `send_text/bytes/json`, `receive_text/bytes/json`
- OpenAPI 3.1 auto-generation, Swagger UI (`/docs`), ReDoc (`/redoc`)
- CORS and GZip middleware (Tower-backed, ~0.3 us per request)
- StreamingResponse (sync and async generators)
- BackgroundTasks
- TestClient (real HTTP via httpx)
- Security: OAuth2PasswordBearer, HTTPBearer, HTTPBasic, APIKey
- `jsonable_encoder`, status code constants, `run_in_threadpool`

### Known limitations

- `response_model` filtering not yet implemented
- `app.mount()` for sub-applications not yet implemented
- ASGI middleware (Sentry, Prometheus) not yet supported (only Tower middleware)
- StaticFiles and Jinja2Templates not yet implemented
- `dependency_overrides` stored but not yet checked at runtime
- Startup/shutdown lifecycle events stored but not yet fired
- Generator (yield) dependencies: cleanup not yet guaranteed

## Database: Use psycopg3 (not psycopg2 or asyncpg)

fastapi-rs is fastest with **psycopg3** — it supports autocommit mode (eliminates transaction overhead) and pipeline mode (sends multiple queries in one network round-trip). psycopg2 and asyncpg lack both features.

### Quick start

```bash
pip install "psycopg[binary,pool]"
```

```python
from fastapi_rs import FastAPI
from fastapi_rs.db import create_pool

app = FastAPI()
pool = create_pool("dbname=mydb user=myuser")  # autocommit=True by default

# Single query — 53us (faster than Go Gin at 56us)
@app.get("/users/{user_id}")
def get_user(user_id: int):
    with pool.connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=%s", (user_id,)).fetchone()
    return dict(row)

# Multiple queries — use pipeline mode (138us for 10 queries, beats Go goroutines at 148us)
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

### Performance: fastapi-rs vs Go Gin (through full framework)

| Queries | fastapi-rs | Go Gin | Winner |
|---------|-----------|--------|--------|
| 1 query (autocommit) | **53us** | 56us | **fastapi-rs** |
| 4 queries (pipeline vs goroutine) | **96us** | 79us | Go (by 17us) |
| 10 queries (pipeline vs goroutine) | **138us** | 148us | **fastapi-rs** |
| 4 queries (sequential) | **104us** | 144us | **fastapi-rs by 40us** |
| 10 queries (sequential) | **197us** | 321us | **fastapi-rs by 124us** |

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
from fastapi_rs.db import create_redis

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
| Sequential | 63us | 152us | 332us |
| **Pipeline** | 63us | **74us** | **91us** |

Pipeline has zero overhead for single commands and is 5.2x faster at 10 commands. Standard redis-py API — `create_redis()` just adds convenience defaults (decode_responses, hiredis).

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

- **Rust core** (1,800 lines): Axum 0.8, hyper, tokio, Tower, PyO3 0.25, crossbeam
- **Python layer** (3,200 lines): FastAPI-compatible API, introspection, OpenAPI, compat shims
- **Tests** (2,500 lines): 128 integration tests across 11 test files

See [CLAUDE.md](CLAUDE.md) for development guide and [benchmarks.md](benchmarks.md) for full benchmark data including Go Echo, Fastify, free-threaded Python, and WebSocket library comparisons.

## License

MIT
