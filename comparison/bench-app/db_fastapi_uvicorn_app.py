"""Database benchmark -- standard FastAPI + uvicorn (no fastapi-turbo).

This is the baseline: standard FastAPI with asyncpg + redis.asyncio,
served by uvicorn. Same endpoints as all other benchmark apps.

Run: python3 db_fastapi_uvicorn_app.py
"""
import asyncpg
import redis.asyncio as aioredis
import json
import os

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="DB Benchmark (uvicorn)")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Global state
pool = None
rc = None


async def get_pool():
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(
            "postgresql://venky@localhost/fastapi_turbo_bench", min_size=5, max_size=20
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


@app.put("/products/{product_id}")
async def update_product(product_id: int, product: ProductCreate):
    db = await get_pool()
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE products SET name=$1, description=$2, price=$3, category_id=$4, stock=$5 "
            "WHERE id=$6 RETURNING id, name, price, stock",
            product.name, product.description, float(product.price),
            product.category_id, product.stock, product_id
        )
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")
    return dict(row)


@app.patch("/products/{product_id}")
async def patch_product(product_id: int, updates: dict):
    db = await get_pool()
    set_clauses = []
    values = []
    idx = 1
    for key in ("name", "description", "price", "category_id", "stock"):
        if key in updates:
            set_clauses.append(f"{key}=${idx}")
            values.append(updates[key])
            idx += 1
    if not set_clauses:
        raise HTTPException(status_code=400, detail="No fields to update")
    values.append(product_id)
    query = f"UPDATE products SET {', '.join(set_clauses)} WHERE id=${idx} RETURNING id, name, price, stock"
    async with db.acquire() as conn:
        row = await conn.fetchrow(query, *values)
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")
    return dict(row)


@app.delete("/products/{product_id}")
async def delete_product(product_id: int):
    db = await get_pool()
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM products WHERE id=$1 RETURNING id", product_id
        )
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"deleted": True, "id": product_id}


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
    import uvicorn
    port = int(os.environ.get("PORT", "19038"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
