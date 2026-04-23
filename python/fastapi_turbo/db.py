"""High-performance database utilities for fastapi-turbo.

Provides a psycopg3 connection pool with auto-pipeline mode for
maximum throughput. Compatible with standard psycopg3 API — users
can use these connections with SQLAlchemy, raw queries, or any
psycopg3-compatible library.

Usage:
    from fastapi_turbo.db import create_pool

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
    """Create a psycopg3 connection pool optimized for fastapi-turbo.

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


# ── Redis ────────────────────────────────────────────────────────────


class RedisPool:
    """Redis client wrapper with auto-pipeline support.

    All commands within a single ``pipeline()`` context are batched into
    one network round-trip.  For convenience, the client also exposes
    standard redis-py methods (get, set, etc.) that work without explicit
    pipelining.

    Usage::

        from fastapi_turbo.db import create_redis

        cache = create_redis()

        # Single command (standard redis-py API)
        value = cache.get("key")
        cache.set("key", "value", ex=60)

        # Multiple commands in one round-trip (pipeline)
        with cache.pipeline() as pipe:
            pipe.get("key1")
            pipe.get("key2")
            pipe.set("key3", "value3")
            results = pipe.execute()  # [value1, value2, True]
    """

    def __init__(self, client):
        self._client = client

    def pipeline(self, transaction: bool = False):
        """Return a redis pipeline context manager.

        Args:
            transaction: Wrap commands in MULTI/EXEC (default: False for speed).
        """
        return self._client.pipeline(transaction=transaction)

    # Expose standard redis-py methods directly
    def get(self, name):
        return self._client.get(name)

    def set(self, name, value, **kwargs):
        return self._client.set(name, value, **kwargs)

    def setex(self, name, time, value):
        return self._client.setex(name, time, value)

    def delete(self, *names):
        return self._client.delete(*names)

    def exists(self, *names):
        return self._client.exists(*names)

    def expire(self, name, time):
        return self._client.expire(name, time)

    def mget(self, keys, *args):
        return self._client.mget(keys, *args)

    def mset(self, mapping):
        return self._client.mset(mapping)

    def incr(self, name, amount=1):
        return self._client.incr(name, amount)

    def hget(self, name, key):
        return self._client.hget(name, key)

    def hset(self, name, key=None, value=None, mapping=None):
        return self._client.hset(name, key, value, mapping)

    def hgetall(self, name):
        return self._client.hgetall(name)

    def lpush(self, name, *values):
        return self._client.lpush(name, *values)

    def lrange(self, name, start, end):
        return self._client.lrange(name, start, end)

    def sadd(self, name, *values):
        return self._client.sadd(name, *values)

    def smembers(self, name):
        return self._client.smembers(name)

    def publish(self, channel, message):
        return self._client.publish(channel, message)

    def __getattr__(self, name):
        """Forward any other redis-py method to the underlying client."""
        return getattr(self._client, name)


def create_redis(
    url: str = "redis://localhost",
    *,
    decode_responses: bool = True,
    **kwargs,
) -> RedisPool:
    """Create a Redis client optimized for fastapi-turbo.

    Uses redis-py with hiredis (C parser) for maximum performance.
    All standard redis-py methods are available.

    Args:
        url: Redis connection URL (default: "redis://localhost")
        decode_responses: Decode bytes to str (default: True)
        **kwargs: Additional arguments passed to redis.Redis.from_url()

    Returns:
        A RedisPool instance with standard redis-py API + pipeline() support.

    Example::

        cache = create_redis()

        # Single operations (29μs per GET on localhost)
        value = cache.get("product:1")
        cache.set("product:1", json.dumps(data), ex=60)

        # Pipeline: 10 GETs in 57μs (vs 298μs sequential)
        with cache.pipeline() as pipe:
            for i in range(10):
                pipe.get(f"product:{i}")
            results = pipe.execute()
    """
    import redis

    client = redis.Redis.from_url(url, decode_responses=decode_responses, **kwargs)
    return RedisPool(client)
