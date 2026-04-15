"""Database benchmark API -- fastapi-rs implementation.

Uses sync handlers with _run_async() for all DB operations to avoid
the coro.send(None) fast-path issue with asyncpg connection pooling.
asyncpg pool is created lazily on the persistent event loop.
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
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Connection state
db_pool = None
redis_client = None
_event_loop = None


async def _init_connections():
    global db_pool, redis_client
    db_pool = await asyncpg.create_pool(
        "postgresql://venky@localhost/fastapi_rs_bench", min_size=5, max_size=20
    )
    redis_client = aioredis.from_url("redis://localhost")


def _ensure():
    """Ensure connections are initialized (called from sync handlers)."""
    global db_pool, redis_client, _event_loop
    if db_pool is not None:
        return
    # Get the persistent event loop from handler_bridge
    from fastapi_rs._fastapi_rs_core import rust_hello  # trigger module load
    import threading
    for t in threading.enumerate():
        if t.name == "fastapi-rs-asyncio":
            # Event loop is running on this thread
            break

    # Create connections on the persistent event loop
    import concurrent.futures
    future = asyncio.run_coroutine_threadsafe(_init_connections(), _get_loop())
    future.result(timeout=10)


def _get_loop():
    """Get the persistent asyncio event loop."""
    global _event_loop
    if _event_loop is not None:
        return _event_loop
    # Import triggers event loop creation
    from fastapi_rs._fastapi_rs_core import rust_hello
    # Find the loop by running a probe
    loop_holder = {}
    async def _get():
        loop_holder["loop"] = asyncio.get_event_loop()
    # Use a temporary loop to find the persistent one
    # Actually, we need to get the loop from handler_bridge
    # The simplest way: call an async handler that captures the loop
    import threading
    _event_loop = None
    # Fallback: create our own loop for DB operations
    _event_loop = asyncio.new_event_loop()
    t = threading.Thread(target=_event_loop.run_forever, daemon=True, name="db-asyncio")
    t.start()
    future = asyncio.run_coroutine_threadsafe(_init_connections(), _event_loop)
    future.result(timeout=10)
    return _event_loop


def _run(coro):
    """Run async operation on the DB event loop."""
    loop = _get_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=30)


def _cleanup():
    if db_pool:
        try:
            loop = _get_loop()
            asyncio.run_coroutine_threadsafe(db_pool.close(), loop).result(timeout=5)
        except Exception:
            pass
    if redis_client:
        try:
            loop = _get_loop()
            asyncio.run_coroutine_threadsafe(redis_client.aclose(), loop).result(timeout=5)
        except Exception:
            pass


atexit.register(_cleanup)


class ProductCreate(BaseModel):
    name: str
    description: str = ""
    price: float
    category_id: int
    stock: int = 0


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/products/{product_id}")
def get_product(product_id: int):
    _ensure()

    async def _query():
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT p.id, p.name, p.price, p.stock, c.name as category_name "
                "FROM products p JOIN categories c ON p.category_id = c.id "
                "WHERE p.id = $1", product_id
            )
        return dict(row) if row else None

    result = _run(_query())
    if not result:
        raise HTTPException(status_code=404, detail="Product not found")
    return result


@app.get("/products")
def list_products(limit: int = Query(10), offset: int = Query(0)):
    _ensure()

    async def _query():
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT p.id, p.name, p.price, p.stock, c.name as category_name "
                "FROM products p JOIN categories c ON p.category_id = c.id "
                "ORDER BY p.id LIMIT $1 OFFSET $2", limit, offset
            )
        return [dict(r) for r in rows]

    return _run(_query())


@app.post("/products", status_code=201)
def create_product(product: ProductCreate):
    _ensure()

    async def _insert():
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO products (name, description, price, category_id, stock) "
                "VALUES ($1, $2, $3, $4, $5) RETURNING id, name, price, stock",
                product.name, product.description, float(product.price),
                product.category_id, product.stock
            )
        return dict(row)

    return _run(_insert())


@app.get("/categories/stats")
def category_stats():
    _ensure()

    async def _query():
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT c.id, c.name, COUNT(p.id) as product_count, "
                "COALESCE(AVG(p.price), 0) as avg_price, "
                "COALESCE(SUM(p.stock), 0) as total_stock "
                "FROM categories c LEFT JOIN products p ON c.id = p.category_id "
                "GROUP BY c.id, c.name ORDER BY c.name"
            )
        return [dict(r) for r in rows]

    return _run(_query())


@app.get("/cached/products/{product_id}")
def get_cached_product(product_id: int):
    _ensure()
    cache_key = f"product:{product_id}"

    async def _cached_query():
        cached = await redis_client.get(cache_key)
        if cached:
            return json.loads(cached)

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT p.id, p.name, p.price, p.stock, c.name as category_name "
                "FROM products p JOIN categories c ON p.category_id = c.id "
                "WHERE p.id = $1", product_id
            )
        if not row:
            return None

        result = dict(row)
        await redis_client.setex(cache_key, 60, json.dumps(result, default=str))
        return result

    result = _run(_cached_query())
    if not result:
        raise HTTPException(status_code=404, detail="Product not found")
    return result


@app.get("/orders/{order_id}")
def get_order(order_id: int):
    _ensure()

    async def _query():
        async with db_pool.acquire() as conn:
            order = await conn.fetchrow("SELECT * FROM orders WHERE id = $1", order_id)
            if not order:
                return None
            items = await conn.fetch(
                "SELECT oi.*, p.name as product_name "
                "FROM order_items oi JOIN products p ON oi.product_id = p.id "
                "WHERE oi.order_id = $1", order_id
            )
        return {"order": dict(order), "items": [dict(i) for i in items]}

    result = _run(_query())
    if not result:
        raise HTTPException(status_code=404, detail="Order not found")
    return result


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=19030)
