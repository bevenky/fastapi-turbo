"""Database benchmark — ASYNC handlers with psycopg3 async + redis.asyncio.
Uses the fastest async drivers (psycopg3 82μs vs asyncpg 147μs).
Standard FastAPI code — users would write this exact same way.
"""
import fastapi_turbo
from fastapi_turbo import FastAPI, Query, HTTPException
from fastapi_turbo.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import psycopg
from psycopg_pool import AsyncConnectionPool
import redis.asyncio as aioredis
import json

app = FastAPI(title="DB Benchmark (async psycopg3)")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

pg_pool = None
redis_client = None


async def get_pool():
    global pg_pool
    if pg_pool is None:
        pg_pool = AsyncConnectionPool(
            "dbname=fastapi_turbo_bench user=venky", min_size=5, max_size=20, open=False
        )
        await pg_pool.open()
        await pg_pool.wait()
    return pg_pool


async def get_redis():
    global redis_client
    if redis_client is None:
        redis_client = aioredis.from_url("redis://localhost")
    return redis_client


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
    pool = await get_pool()
    async with pool.connection() as conn:
        row = (await conn.execute(
            "SELECT p.id, p.name, p.price, p.stock, c.name as category_name "
            "FROM products p JOIN categories c ON p.category_id = c.id "
            "WHERE p.id = %s", (product_id,)
        )).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"id": row[0], "name": row[1], "price": float(row[2]), "stock": row[3], "category_name": row[4]}


@app.get("/products")
async def list_products(limit: int = Query(10), offset: int = Query(0)):
    pool = await get_pool()
    async with pool.connection() as conn:
        rows = (await conn.execute(
            "SELECT p.id, p.name, p.price, p.stock, c.name as category_name "
            "FROM products p JOIN categories c ON p.category_id = c.id "
            "ORDER BY p.id LIMIT %s OFFSET %s", (limit, offset)
        )).fetchall()
    return [{"id": r[0], "name": r[1], "price": float(r[2]), "stock": r[3], "category_name": r[4]} for r in rows]


@app.post("/products", status_code=201)
async def create_product(product: ProductCreate):
    pool = await get_pool()
    async with pool.connection() as conn:
        row = (await conn.execute(
            "INSERT INTO products (name, description, price, category_id, stock) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id, name, price, stock",
            (product.name, product.description, float(product.price), product.category_id, product.stock)
        )).fetchone()
        await conn.commit()
    return {"id": row[0], "name": row[1], "price": float(row[2]), "stock": row[3]}


@app.get("/categories/stats")
async def category_stats():
    pool = await get_pool()
    async with pool.connection() as conn:
        rows = (await conn.execute(
            "SELECT c.id, c.name, COUNT(p.id) as product_count, "
            "COALESCE(AVG(p.price), 0) as avg_price, COALESCE(SUM(p.stock), 0) as total_stock "
            "FROM categories c LEFT JOIN products p ON c.id = p.category_id "
            "GROUP BY c.id, c.name ORDER BY c.name"
        )).fetchall()
    return [{"id": r[0], "name": r[1], "product_count": r[2], "avg_price": float(r[3]), "total_stock": r[4]} for r in rows]


@app.get("/cached/products/{product_id}")
async def get_cached_product(product_id: int):
    pool = await get_pool()
    rc = await get_redis()
    cache_key = f"product:{product_id}"
    cached = await rc.get(cache_key)
    if cached:
        return json.loads(cached)
    async with pool.connection() as conn:
        row = (await conn.execute(
            "SELECT p.id, p.name, p.price, p.stock, c.name as category_name "
            "FROM products p JOIN categories c ON p.category_id = c.id "
            "WHERE p.id = %s", (product_id,)
        )).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")
    result = {"id": row[0], "name": row[1], "price": float(row[2]), "stock": row[3], "category_name": row[4]}
    await rc.setex(cache_key, 60, json.dumps(result))
    return result


@app.get("/orders/{order_id}")
async def get_order(order_id: int):
    pool = await get_pool()
    async with pool.connection() as conn:
        order = (await conn.execute("SELECT * FROM orders WHERE id = %s", (order_id,))).fetchone()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        items = (await conn.execute(
            "SELECT oi.*, p.name as product_name FROM order_items oi "
            "JOIN products p ON oi.product_id = p.id WHERE oi.order_id = %s", (order_id,)
        )).fetchall()
    return {
        "order": {"id": order[0], "user_id": order[1], "total": float(order[2]), "status": order[3]},
        "items": [{"id": i[0], "order_id": i[1], "product_id": i[2], "quantity": i[3], "unit_price": float(i[4]), "product_name": i[5]} for i in items]
    }


if __name__ == "__main__":
    import os; app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 19036)))
