"""Database benchmark API -- fastapi-rs implementation.

Tests real async PostgreSQL (asyncpg) and Redis (redis.asyncio) performance.
Exercises: JOINs, pagination, aggregation, INSERT RETURNING, Redis caching,
multi-table queries.

Architecture: startup event stores config; pools lazily created on first
async request (bound to the persistent event loop that handles requests).
POST handler uses sync + run_coroutine_threadsafe because the Rust core
cannot re-extract body params in the async fallback path.
"""

import fastapi_rs
from fastapi_rs import FastAPI, Query, HTTPException
from fastapi_rs.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncpg
import asyncio
import redis.asyncio as aioredis
import json
import atexit

app = FastAPI(title="DB Benchmark (fastapi-rs)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Connection state
db_pool = None
redis_client = None
_init_lock = None
_event_loop = None


async def _ensure_connections():
    """Lazily create asyncpg pool + redis on the persistent request loop."""
    global db_pool, redis_client, _init_lock, _event_loop
    if db_pool is not None and redis_client is not None:
        return
    _event_loop = asyncio.get_event_loop()
    if _init_lock is None:
        _init_lock = asyncio.Lock()
    async with _init_lock:
        if db_pool is None:
            db_pool = await asyncpg.create_pool(
                "postgresql://venky@localhost/fastapi_rs_bench",
                min_size=5,
                max_size=20,
            )
        if redis_client is None:
            redis_client = aioredis.from_url("redis://localhost")


def _run_async(coro):
    """Run coroutine on persistent loop from sync context (for POST handlers)."""
    if _event_loop is None or _event_loop.is_closed():
        raise RuntimeError("Event loop not initialized -- hit a GET endpoint first")
    future = asyncio.run_coroutine_threadsafe(coro, _event_loop)
    return future.result(timeout=30)


def _cleanup():
    if db_pool:
        try:
            if _event_loop and not _event_loop.is_closed():
                asyncio.run_coroutine_threadsafe(db_pool.close(), _event_loop).result(timeout=5)
        except Exception:
            pass
    if redis_client:
        try:
            if _event_loop and not _event_loop.is_closed():
                asyncio.run_coroutine_threadsafe(redis_client.close(), _event_loop).result(timeout=5)
        except Exception:
            pass


atexit.register(_cleanup)


# ── Models ────────────────────────────────────────────────────────────


class ProductCreate(BaseModel):
    name: str
    description: str = ""
    price: float
    category_id: int
    stock: int = 0


# ── Endpoints ─────────────────────────────────────────────────────────


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/products/{product_id}")
async def get_product(product_id: int):
    await _ensure_connections()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT p.id, p.name, p.price, p.stock, c.name as category_name "
            "FROM products p JOIN categories c ON p.category_id = c.id "
            "WHERE p.id = $1",
            product_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")
    return dict(row)


@app.get("/products")
async def list_products(limit: int = Query(10), offset: int = Query(0)):
    await _ensure_connections()
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT p.id, p.name, p.price, p.stock, c.name as category_name "
            "FROM products p JOIN categories c ON p.category_id = c.id "
            "ORDER BY p.id LIMIT $1 OFFSET $2",
            limit,
            offset,
        )
    return [dict(r) for r in rows]


@app.post("/products", status_code=201)
def create_product(product: ProductCreate):
    async def _insert():
        await _ensure_connections()
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO products (name, description, price, category_id, stock) "
                "VALUES ($1, $2, $3, $4, $5) RETURNING id, name, price, stock",
                product.name,
                product.description,
                product.price,
                product.category_id,
                product.stock,
            )
        return dict(row)
    return _run_async(_insert())


@app.get("/categories/stats")
async def category_stats():
    await _ensure_connections()
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT c.id, c.name, COUNT(p.id) as product_count, "
            "COALESCE(AVG(p.price), 0) as avg_price, "
            "COALESCE(SUM(p.stock), 0) as total_stock "
            "FROM categories c LEFT JOIN products p ON c.id = p.category_id "
            "GROUP BY c.id, c.name ORDER BY c.name"
        )
    return [dict(r) for r in rows]


@app.get("/cached/products/{product_id}")
async def get_cached_product(product_id: int):
    await _ensure_connections()
    cache_key = f"product:{product_id}"

    cached = await redis_client.get(cache_key)
    if cached:
        return json.loads(cached)

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT p.id, p.name, p.price, p.stock, c.name as category_name "
            "FROM products p JOIN categories c ON p.category_id = c.id "
            "WHERE p.id = $1",
            product_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")

    result = dict(row)
    await redis_client.setex(cache_key, 60, json.dumps(result, default=str))
    return result


@app.get("/orders/{order_id}")
async def get_order(order_id: int):
    await _ensure_connections()
    async with db_pool.acquire() as conn:
        order = await conn.fetchrow(
            "SELECT * FROM orders WHERE id = $1", order_id
        )
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        items = await conn.fetch(
            "SELECT oi.*, p.name as product_name "
            "FROM order_items oi "
            "JOIN products p ON oi.product_id = p.id "
            "WHERE oi.order_id = $1",
            order_id,
        )
    return {
        "order": dict(order),
        "items": [dict(i) for i in items],
    }


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=19030)
