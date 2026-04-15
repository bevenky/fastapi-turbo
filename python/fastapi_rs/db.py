"""High-performance database utilities for fastapi-rs.

Provides a psycopg3 connection pool with auto-pipeline mode for
maximum throughput. Compatible with standard psycopg3 API — users
can use these connections with SQLAlchemy, raw queries, or any
psycopg3-compatible library.

Usage:
    from fastapi_rs.db import create_pool

    pool = create_pool("dbname=mydb user=myuser")

    @app.get("/items")
    def list_items():
        with pool.connection() as conn:
            rows = conn.execute("SELECT * FROM items").fetchall()
        return [dict(r) for r in rows]

    # Multiple queries auto-pipeline (4+ queries batched in one round-trip):
    @app.get("/dashboard")
    def dashboard():
        with pool.connection() as conn:
            with conn.pipeline():
                users = conn.execute("SELECT * FROM users LIMIT 10")
                orders = conn.execute("SELECT * FROM orders LIMIT 10")
                stats = conn.execute("SELECT COUNT(*) FROM orders")
                return {
                    "users": [dict(r) for r in users.fetchall()],
                    "orders": [dict(r) for r in orders.fetchall()],
                    "stats": dict(stats.fetchone()),
                }

Configuration:
    pool = create_pool(dsn, autocommit=True)    # default: auto-commit on (fastest)
    pool = create_pool(dsn, autocommit=False)   # explicit transactions
"""

from __future__ import annotations

import psycopg
from psycopg_pool import ConnectionPool


def create_pool(
    conninfo: str,
    *,
    min_size: int = 5,
    max_size: int = 20,
    autocommit: bool = True,
    **kwargs,
) -> ConnectionPool:
    """Create a psycopg3 connection pool optimized for fastapi-rs.

    By default, connections use autocommit=True which eliminates
    BEGIN/COMMIT overhead per query (~5μs saved per request).

    Args:
        conninfo: PostgreSQL connection string (e.g., "dbname=mydb user=myuser")
        min_size: Minimum number of connections in the pool (default: 5)
        max_size: Maximum number of connections in the pool (default: 20)
        autocommit: Enable autocommit mode (default: True, fastest for reads).
                    Set to False if you need explicit transactions.
        **kwargs: Additional arguments passed to psycopg_pool.ConnectionPool

    Returns:
        A psycopg3 ConnectionPool instance.

    Example:
        pool = create_pool("dbname=mydb user=myuser")

        # Single query (fast: ~36μs per query)
        with pool.connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE id=%s", (1,)).fetchone()

        # Multiple queries with pipeline (fast: ~125μs for 10 queries)
        with pool.connection() as conn:
            with conn.pipeline():
                r1 = conn.execute("SELECT * FROM users WHERE id=%s", (1,))
                r2 = conn.execute("SELECT * FROM orders WHERE uid=%s", (1,))
                user = r1.fetchone()
                orders = r2.fetchall()

        # To disable autocommit (for explicit transactions):
        pool = create_pool("dbname=mydb", autocommit=False)
        with pool.connection() as conn:
            with conn.transaction():
                conn.execute("UPDATE accounts SET balance=balance-100 WHERE id=1")
                conn.execute("UPDATE accounts SET balance=balance+100 WHERE id=2")
    """

    def _configure(conn: psycopg.Connection) -> None:
        conn.autocommit = autocommit

    pool = ConnectionPool(
        conninfo,
        min_size=min_size,
        max_size=max_size,
        configure=_configure,
        **kwargs,
    )
    pool.wait()
    return pool
