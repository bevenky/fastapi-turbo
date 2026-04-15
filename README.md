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

## Database Performance Tips

fastapi-rs supports both sync and async handlers. For database-heavy applications, the choice of driver and handler style has a significant impact on performance.

### Recommended: sync handlers with psycopg3

```python
from fastapi_rs import FastAPI
import psycopg
from psycopg_pool import ConnectionPool

app = FastAPI()
pool = ConnectionPool("dbname=mydb user=myuser", min_size=5, max_size=20)

@app.get("/users/{user_id}")
def get_user(user_id: int):
    with pool.connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
    return dict(row)
```

Sync handlers run on `block_in_place` — tokio migrates other tasks to other workers while this thread blocks on the DB call. This is the same pattern Go uses with goroutines. No event loop overhead.

### Async with psycopg3 (when you need parallel I/O)

```python
@app.get("/dashboard")
async def dashboard(user_id: int):
    pool = await get_pool()
    async with pool.connection() as conn:
        user = (await conn.execute("SELECT * FROM users WHERE id = %s", (user_id,))).fetchone()
        orders = (await conn.execute("SELECT * FROM orders WHERE user_id = %s", (user_id,))).fetchone()
    return {"user": dict(user), "orders": dict(orders)}
```

Use `async def` when your handler needs to do multiple I/O operations concurrently (e.g., `asyncio.gather`). fastapi-rs uses uvloop automatically for faster async scheduling.

### Driver performance comparison

| Driver | Mode | Latency per query | When to use |
|--------|------|-------------------|-------------|
| **psycopg3** | sync | **46 us** | Default choice — fastest |
| psycopg2 | sync | 48 us | Legacy, widely compatible |
| psycopg3 | async | 82 us | Parallel I/O within handler |
| asyncpg | async | 147 us | Avoid — 3x slower than psycopg3 sync |
| SQLAlchemy Core | sync | 117 us | When you need ORM features |

### Redis

| Driver | Mode | Latency per GET | When to use |
|--------|------|-----------------|-------------|
| **redis-py** | sync | **28 us** | Default — fastest |
| redis.asyncio | async | 53 us | Only for parallel I/O |

### Why sync is faster

asyncio's `run_until_complete` adds ~29us of event loop overhead per call, even for trivial coroutines. For sequential database operations (the common case), this overhead is pure waste. Sync drivers make direct socket calls with zero event loop overhead.

fastapi-rs handles concurrency through tokio's multi-threaded runtime — each sync handler blocks one tokio worker thread, while other workers continue serving requests. This matches Go's goroutine model.

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
