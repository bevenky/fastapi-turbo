"""Database benchmark — standard FastAPI code, zero framework-specific patterns.

Uses psycopg3 sync API. fastapi-rs runs sync handlers on tokio blocking
threads, so sync DB calls are natural and avoid all event-loop issues.
"""
from fastapi_rs import FastAPI, Query, Body, HTTPException
from fastapi_rs.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from psycopg_pool import ConnectionPool
import redis
import json

app = FastAPI(title="DB Benchmark")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Standard psycopg3 pool — autocommit is automatically enabled by
# fastapi-rs's import hook (no special config needed).
DSN = "postgresql://venky@localhost/fastapi_rs_bench"
pool = ConnectionPool(DSN, min_size=5, max_size=20)
rc = redis.from_url("redis://localhost")


class ProductCreate(BaseModel):
    name: str
    description: str = ""
    price: float
    category_id: int
    stock: int = 0


def _rows(cur):
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _row(cur):
    cols = [d.name for d in cur.description]
    r = cur.fetchone()
    return dict(zip(cols, r)) if r else None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/products/{product_id}")
def get_product(product_id: int):
    with pool.connection() as conn:
        cur = conn.execute(
            "SELECT p.id, p.name, p.price, p.stock, c.name as category_name "
            "FROM products p JOIN categories c ON p.category_id = c.id "
            "WHERE p.id = %s", (product_id,)
        )
        row = _row(cur)
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")
    return row


@app.get("/products")
def list_products(limit: int = Query(10), offset: int = Query(0)):
    with pool.connection() as conn:
        cur = conn.execute(
            "SELECT p.id, p.name, p.price, p.stock, c.name as category_name "
            "FROM products p JOIN categories c ON p.category_id = c.id "
            "ORDER BY p.id LIMIT %s OFFSET %s", (limit, offset)
        )
        return _rows(cur)


@app.post("/products", status_code=201)
def create_product(product: ProductCreate):
    with pool.connection() as conn:
        cur = conn.execute(
            "INSERT INTO products (name, description, price, category_id, stock) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id, name, price, stock",
            (product.name, product.description, float(product.price),
             product.category_id, product.stock)
        )
        row = _row(cur)
        pass  # autocommit handles commit
    return row


@app.put("/products/{product_id}")
def update_product(product_id: int, product: ProductCreate):
    with pool.connection() as conn:
        cur = conn.execute(
            "UPDATE products SET name=%s, description=%s, price=%s, category_id=%s, stock=%s "
            "WHERE id=%s RETURNING id, name, price, stock",
            (product.name, product.description, float(product.price),
             product.category_id, product.stock, product_id)
        )
        row = _row(cur)
        pass  # autocommit handles commit
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")
    return row


@app.patch("/products/{product_id}")
def patch_product(product_id: int, updates: dict = Body(...)):
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
    with pool.connection() as conn:
        cur = conn.execute(query, tuple(values))
        row = _row(cur)
        pass  # autocommit handles commit
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")
    return row


@app.delete("/products/{product_id}")
def delete_product(product_id: int):
    with pool.connection() as conn:
        cur = conn.execute(
            "DELETE FROM products WHERE id=%s RETURNING id", (product_id,)
        )
        row = _row(cur)
        pass  # autocommit handles commit
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"deleted": True, "id": product_id}


@app.get("/categories")
def list_categories():
    with pool.connection() as conn:
        cur = conn.execute("SELECT id, name FROM categories ORDER BY name")
        return _rows(cur)


@app.get("/categories/stats")
def category_stats():
    with pool.connection() as conn:
        cur = conn.execute(
            "SELECT c.id, c.name, COUNT(p.id) as product_count, "
            "COALESCE(AVG(p.price), 0) as avg_price, "
            "COALESCE(SUM(p.stock), 0) as total_stock "
            "FROM categories c LEFT JOIN products p ON c.id = p.category_id "
            "GROUP BY c.id, c.name ORDER BY c.name"
        )
        return _rows(cur)


@app.get("/cached/products/{product_id}")
def get_cached_product(product_id: int):
    cache_key = f"product:{product_id}"
    cached = rc.get(cache_key)
    if cached:
        return json.loads(cached if isinstance(cached, (bytes, str)) else str(cached))

    with pool.connection() as conn:
        cur = conn.execute(
            "SELECT p.id, p.name, p.price, p.stock, c.name as category_name "
            "FROM products p JOIN categories c ON p.category_id = c.id "
            "WHERE p.id = %s", (product_id,)
        )
        row = _row(cur)
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")

    rc.setex(cache_key, 60, json.dumps(row, default=str))
    return row


@app.get("/orders/{order_id}")
def get_order(order_id: int):
    with pool.connection() as conn:
        cur = conn.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
        order = _row(cur)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        cur2 = conn.execute(
            "SELECT oi.*, p.name as product_name "
            "FROM order_items oi JOIN products p ON oi.product_id = p.id "
            "WHERE oi.order_id = %s", (order_id,)
        )
        items = _rows(cur2)
    return {"order": order, "items": items}


@app.get("/products/1/details")
def product_details():
    """JOIN query for benchmarking."""
    with pool.connection() as conn:
        cur = conn.execute(
            "SELECT p.id, p.name, p.price, p.stock, c.name as category_name "
            "FROM products p JOIN categories c ON p.category_id = c.id "
            "WHERE p.id = 1"
        )
        return _row(cur) or {}


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=19030)
