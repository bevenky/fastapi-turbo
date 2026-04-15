"""Database benchmark — standard FastAPI code, zero framework-specific patterns.

This is EXACTLY how you'd write a FastAPI app. The framework handles everything.
"""
import fastapi_rs
from fastapi_rs import FastAPI, Query, HTTPException
from fastapi_rs.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncpg
import redis.asyncio as aioredis
import json

app = FastAPI(title="DB Benchmark")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Global state — initialized on first request (standard FastAPI pattern)
pool = None
rc = None


async def get_pool():
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(
            "postgresql://venky@localhost/fastapi_rs_bench", min_size=5, max_size=20
        )
    return pool


async def get_redis():
    global rc
    if rc is None:
        rc = aioredis.from_url("redis://localhost")
    return rc


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
async def get_product(product_id: int):
    db = await get_pool()
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT p.id, p.name, p.price, p.stock, c.name as category_name "
            "FROM products p JOIN categories c ON p.category_id = c.id "
            "WHERE p.id = $1", product_id
        )
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")
    return dict(row)


@app.get("/products")
async def list_products(limit: int = Query(10), offset: int = Query(0)):
    db = await get_pool()
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT p.id, p.name, p.price, p.stock, c.name as category_name "
            "FROM products p JOIN categories c ON p.category_id = c.id "
            "ORDER BY p.id LIMIT $1 OFFSET $2", limit, offset
        )
    return [dict(r) for r in rows]


@app.post("/products", status_code=201)
async def create_product(product: ProductCreate):
    db = await get_pool()
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO products (name, description, price, category_id, stock) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING id, name, price, stock",
            product.name, product.description, float(product.price),
            product.category_id, product.stock
        )
    return dict(row)


@app.get("/categories/stats")
async def category_stats():
    db = await get_pool()
    async with db.acquire() as conn:
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
    db = await get_pool()
    cache = await get_redis()
    cache_key = f"product:{product_id}"

    cached = await cache.get(cache_key)
    if cached:
        return json.loads(cached)

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT p.id, p.name, p.price, p.stock, c.name as category_name "
            "FROM products p JOIN categories c ON p.category_id = c.id "
            "WHERE p.id = $1", product_id
        )
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")

    result = dict(row)
    await cache.setex(cache_key, 60, json.dumps(result, default=str))
    return result


@app.get("/orders/{order_id}")
async def get_order(order_id: int):
    db = await get_pool()
    async with db.acquire() as conn:
        order = await conn.fetchrow("SELECT * FROM orders WHERE id = $1", order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        items = await conn.fetch(
            "SELECT oi.*, p.name as product_name "
            "FROM order_items oi JOIN products p ON oi.product_id = p.id "
            "WHERE oi.order_id = $1", order_id
        )
    return {"order": dict(order), "items": [dict(i) for i in items]}


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=19030)
