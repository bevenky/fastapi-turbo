"""Database benchmark — SYNC handlers with psycopg2 + redis-py.

This is the FAST path: sync handlers run on block_in_place, DB calls
block with GIL released. Zero event loop overhead.

Matches how Go works — pgx blocks the goroutine on socket read.
"""
import fastapi_rs
from fastapi_rs import FastAPI, Query, Body, HTTPException
from fastapi_rs.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import psycopg2
from psycopg2.pool import ThreadedConnectionPool
import redis
import json

app = FastAPI(title="DB Benchmark (sync)")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Sync connection pools
db_pool = ThreadedConnectionPool(5, 20, "dbname=fastapi_rs_bench user=venky")
redis_client = redis.Redis(host="localhost", port=6379, decode_responses=True)


def get_conn():
    return db_pool.getconn()


def put_conn(conn):
    db_pool.putconn(conn)


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
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT p.id, p.name, p.price, p.stock, c.name as category_name "
            "FROM products p JOIN categories c ON p.category_id = c.id "
            "WHERE p.id = %s", (product_id,)
        )
        row = cur.fetchone()
        cur.close()
    finally:
        put_conn(conn)
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"id": row[0], "name": row[1], "price": float(row[2]), "stock": row[3], "category_name": row[4]}


@app.get("/products")
def list_products(limit: int = Query(10), offset: int = Query(0)):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT p.id, p.name, p.price, p.stock, c.name as category_name "
            "FROM products p JOIN categories c ON p.category_id = c.id "
            "ORDER BY p.id LIMIT %s OFFSET %s", (limit, offset)
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        put_conn(conn)
    return [{"id": r[0], "name": r[1], "price": float(r[2]), "stock": r[3], "category_name": r[4]} for r in rows]


@app.post("/products", status_code=201)
def create_product(product: ProductCreate):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO products (name, description, price, category_id, stock) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id, name, price, stock",
            (product.name, product.description, float(product.price), product.category_id, product.stock)
        )
        row = cur.fetchone()
        conn.commit()
        cur.close()
    finally:
        put_conn(conn)
    return {"id": row[0], "name": row[1], "price": float(row[2]), "stock": row[3]}


@app.put("/products/{product_id}")
def update_product(product_id: int, product: ProductCreate):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE products SET name=%s, description=%s, price=%s, category_id=%s, stock=%s "
            "WHERE id=%s RETURNING id, name, price, stock",
            (product.name, product.description, float(product.price),
             product.category_id, product.stock, product_id)
        )
        row = cur.fetchone()
        conn.commit()
        cur.close()
    finally:
        put_conn(conn)
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"id": row[0], "name": row[1], "price": float(row[2]), "stock": row[3]}


@app.patch("/products/{product_id}")
def patch_product(product_id: int, updates: dict = Body(...)):
    conn = get_conn()
    try:
        set_clauses = []
        values = []
        for key in ("name", "description", "price", "category_id", "stock"):
            if key in updates:
                set_clauses.append(f"{key}=%s")
                values.append(updates[key])
        if not set_clauses:
            raise HTTPException(status_code=400, detail="No fields to update")
        values.append(product_id)
        query = f"UPDATE products SET {', '.join(set_clauses)} WHERE id=%s RETURNING id, name, price, stock"
        cur = conn.cursor()
        cur.execute(query, values)
        row = cur.fetchone()
        conn.commit()
        cur.close()
    finally:
        put_conn(conn)
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"id": row[0], "name": row[1], "price": float(row[2]), "stock": row[3]}


@app.delete("/products/{product_id}")
def delete_product(product_id: int):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM products WHERE id=%s RETURNING id", (product_id,))
        row = cur.fetchone()
        conn.commit()
        cur.close()
    finally:
        put_conn(conn)
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"deleted": True, "id": product_id}


@app.get("/categories/stats")
def category_stats():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT c.id, c.name, COUNT(p.id) as product_count, "
            "COALESCE(AVG(p.price), 0) as avg_price, "
            "COALESCE(SUM(p.stock), 0) as total_stock "
            "FROM categories c LEFT JOIN products p ON c.id = p.category_id "
            "GROUP BY c.id, c.name ORDER BY c.name"
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        put_conn(conn)
    return [{"id": r[0], "name": r[1], "product_count": r[2], "avg_price": float(r[3]), "total_stock": r[4]} for r in rows]


@app.get("/cached/products/{product_id}")
def get_cached_product(product_id: int):
    cache_key = f"product:{product_id}"
    cached = redis_client.get(cache_key)
    if cached:
        return json.loads(cached)

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT p.id, p.name, p.price, p.stock, c.name as category_name "
            "FROM products p JOIN categories c ON p.category_id = c.id "
            "WHERE p.id = %s", (product_id,)
        )
        row = cur.fetchone()
        cur.close()
    finally:
        put_conn(conn)
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")
    result = {"id": row[0], "name": row[1], "price": float(row[2]), "stock": row[3], "category_name": row[4]}
    redis_client.setex(cache_key, 60, json.dumps(result))
    return result


@app.get("/orders/{order_id}")
def get_order(order_id: int):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
        order = cur.fetchone()
        if not order:
            cur.close()
            put_conn(conn)
            raise HTTPException(status_code=404, detail="Order not found")
        cur.execute(
            "SELECT oi.*, p.name as product_name "
            "FROM order_items oi JOIN products p ON oi.product_id = p.id "
            "WHERE oi.order_id = %s", (order_id,)
        )
        items = cur.fetchall()
        cur.close()
    finally:
        put_conn(conn)
    return {
        "order": {"id": order[0], "user_id": order[1], "total": float(order[2]), "status": order[3]},
        "items": [{"id": i[0], "order_id": i[1], "product_id": i[2], "quantity": i[3], "unit_price": float(i[4]), "product_name": i[5]} for i in items]
    }


if __name__ == "__main__":
    import os; app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 19035)))
