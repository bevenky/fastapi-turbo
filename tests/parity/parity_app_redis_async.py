"""Redis integration parity app (ASYNC redis-py).

Mirrors parity_app_redis.py but uses redis.asyncio.Redis. Exercises the async
API end-to-end through the full FastAPI event-loop / handler bridge.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Annotated, Any, Optional

from fastapi import (
    FastAPI,
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Body,
)
from pydantic import BaseModel

import redis.asyncio as aioredis
from redis.asyncio.connection import ConnectionPool
import redis


REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6392"))
REDIS_DB = int(os.environ.get("REDIS_DB", "0"))


# Lazily-initialized pools. We can't build them at import time because the
# event loop doesn't exist yet in stock FastAPI's pre-lifespan context.
_POOL: Optional[ConnectionPool] = None
_RAW_POOL: Optional[ConnectionPool] = None


def _pool() -> ConnectionPool:
    global _POOL
    if _POOL is None:
        _POOL = ConnectionPool.from_url(
            f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}",
            decode_responses=True, max_connections=128,
        )
    return _POOL


def _raw_pool() -> ConnectionPool:
    global _RAW_POOL
    if _RAW_POOL is None:
        _RAW_POOL = ConnectionPool.from_url(
            f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}",
            decode_responses=False, max_connections=64,
        )
    return _RAW_POOL


async def get_redis() -> aioredis.Redis:
    r = aioredis.Redis(connection_pool=_pool())
    try:
        yield r
    finally:
        # Connection returns to pool automatically.
        pass


def get_raw() -> aioredis.Redis:
    return aioredis.Redis(connection_pool=_raw_pool())


RDep = Annotated[aioredis.Redis, Depends(get_redis)]


app = FastAPI(title="Redis Parity App (async)", version="1.0.0")


@app.get("/health")
async def health():
    try:
        r = aioredis.Redis(connection_pool=_pool())
        await r.ping()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


# =====================================================================
# STRINGS
# =====================================================================
strings = APIRouter(prefix="/str", tags=["strings"])


class SetBody(BaseModel):
    value: str
    ex: Optional[int] = None
    nx: bool = False
    xx: bool = False


@strings.post("/set")
async def str_set(k: str, body: SetBody, r: RDep):
    ok = await r.set(k, body.value, ex=body.ex, nx=body.nx or None, xx=body.xx or None)
    return {"ok": bool(ok)}


@strings.get("/get")
async def str_get(k: str, r: RDep):
    return {"value": await r.get(k)}


@strings.post("/getset")
async def str_getset(k: str, v: str, r: RDep):
    return {"prev": await r.getset(k, v)}


@strings.post("/setex")
async def str_setex(k: str, seconds: int, v: str, r: RDep):
    await r.setex(k, seconds, v)
    return {"ok": True, "ttl": await r.ttl(k)}


@strings.post("/psetex")
async def str_psetex(k: str, ms: int, v: str, r: RDep):
    await r.psetex(k, ms, v)
    return {"ok": True, "pttl_gt0": (await r.pttl(k) or 0) > 0}


@strings.post("/setnx")
async def str_setnx(k: str, v: str, r: RDep):
    return {"created": bool(await r.setnx(k, v))}


@strings.post("/mset")
async def str_mset(mapping: Annotated[dict[str, str], Body()], r: RDep):
    await r.mset(mapping)
    return {"ok": True, "count": len(mapping)}


@strings.get("/mget")
async def str_mget(keys: Annotated[list[str], Query()], r: RDep):
    return {"values": await r.mget(keys)}


@strings.get("/getrange")
async def str_getrange(k: str, start: int, end: int, r: RDep):
    return {"value": await r.getrange(k, start, end)}


@strings.post("/setrange")
async def str_setrange(k: str, offset: int, v: str, r: RDep):
    return {"new_length": await r.setrange(k, offset, v)}


@strings.get("/strlen")
async def str_strlen(k: str, r: RDep):
    return {"length": await r.strlen(k)}


@strings.post("/incr")
async def str_incr(k: str, r: RDep):
    return {"value": await r.incr(k)}


@strings.post("/decr")
async def str_decr(k: str, r: RDep):
    return {"value": await r.decr(k)}


@strings.post("/incrby")
async def str_incrby(k: str, n: int, r: RDep):
    return {"value": await r.incrby(k, n)}


@strings.post("/decrby")
async def str_decrby(k: str, n: int, r: RDep):
    return {"value": await r.decrby(k, n)}


@strings.post("/incrbyfloat")
async def str_incrbyfloat(k: str, n: float, r: RDep):
    return {"value": float(await r.incrbyfloat(k, n))}


@strings.post("/append")
async def str_append(k: str, v: str, r: RDep):
    return {"new_length": await r.append(k, v)}


@strings.post("/expire")
async def str_expire(k: str, seconds: int, r: RDep):
    return {"applied": bool(await r.expire(k, seconds))}


@strings.post("/expireat")
async def str_expireat(k: str, ts: int, r: RDep):
    return {"applied": bool(await r.expireat(k, ts))}


@strings.post("/persist")
async def str_persist(k: str, r: RDep):
    return {"applied": bool(await r.persist(k))}


@strings.get("/ttl")
async def str_ttl(k: str, r: RDep):
    return {"ttl": await r.ttl(k)}


@strings.get("/pttl")
async def str_pttl(k: str, r: RDep):
    p = await r.pttl(k)
    if p is None or p < 0:
        return {"pttl_bucket": p}
    return {"pttl_bucket": "positive"}


@strings.get("/type")
async def str_type(k: str, r: RDep):
    return {"type": await r.type(k)}


@strings.get("/exists")
async def str_exists(keys: Annotated[list[str], Query()], r: RDep):
    return {"count": await r.exists(*keys)}


@strings.post("/del")
async def str_del(keys: Annotated[list[str], Query()], r: RDep):
    return {"deleted": await r.delete(*keys)}


@strings.post("/unlink")
async def str_unlink(keys: Annotated[list[str], Query()], r: RDep):
    return {"unlinked": await r.unlink(*keys)}


@strings.get("/keys")
async def str_keys(pattern: str, r: RDep):
    return {"keys": sorted(await r.keys(pattern))}


@strings.get("/scan")
async def str_scan(match: str = "*", count: int = 100, r: RDep = None):  # type: ignore[assignment]
    cur = 0
    found: list[str] = []
    while True:
        cur, batch = await r.scan(cursor=cur, match=match, count=count)
        found.extend(batch)
        if cur == 0:
            break
    return {"keys": sorted(set(found))}


app.include_router(strings)


# =====================================================================
# LISTS
# =====================================================================
lists = APIRouter(prefix="/list", tags=["lists"])


@lists.post("/lpush")
async def list_lpush(k: str, vals: Annotated[list[str], Query(alias="v")], r: RDep):
    return {"length": await r.lpush(k, *vals)}


@lists.post("/rpush")
async def list_rpush(k: str, vals: Annotated[list[str], Query(alias="v")], r: RDep):
    return {"length": await r.rpush(k, *vals)}


@lists.post("/lpop")
async def list_lpop(k: str, count: Optional[int] = None, r: RDep = None):  # type: ignore[assignment]
    return {"value": await (r.lpop(k, count) if count else r.lpop(k))}


@lists.post("/rpop")
async def list_rpop(k: str, count: Optional[int] = None, r: RDep = None):  # type: ignore[assignment]
    return {"value": await (r.rpop(k, count) if count else r.rpop(k))}


@lists.get("/lrange")
async def list_lrange(k: str, start: int = 0, stop: int = -1, r: RDep = None):  # type: ignore[assignment]
    return {"values": await r.lrange(k, start, stop)}


@lists.get("/llen")
async def list_llen(k: str, r: RDep):
    return {"length": await r.llen(k)}


@lists.get("/lindex")
async def list_lindex(k: str, i: int, r: RDep):
    return {"value": await r.lindex(k, i)}


@lists.post("/lset")
async def list_lset(k: str, i: int, v: str, r: RDep):
    try:
        await r.lset(k, i, v)
        return {"ok": True}
    except redis.ResponseError as e:
        raise HTTPException(status_code=400, detail=str(e))


@lists.post("/linsert")
async def list_linsert(k: str, where: str, pivot: str, v: str, r: RDep):
    return {"length": await r.linsert(k, where.upper(), pivot, v)}


@lists.post("/lrem")
async def list_lrem(k: str, count: int, v: str, r: RDep):
    return {"removed": await r.lrem(k, count, v)}


@lists.post("/ltrim")
async def list_ltrim(k: str, start: int, stop: int, r: RDep):
    await r.ltrim(k, start, stop)
    return {"ok": True}


@lists.post("/rpoplpush")
async def list_rpoplpush(src: str, dst: str, r: RDep):
    return {"value": await r.rpoplpush(src, dst)}


@lists.post("/lmove")
async def list_lmove(src: str, dst: str, where_from: str = "LEFT", where_to: str = "RIGHT", r: RDep = None):  # type: ignore[assignment]
    return {"value": await r.lmove(src, dst, where_from.upper(), where_to.upper())}


@lists.post("/blpop")
async def list_blpop(keys: Annotated[list[str], Query()], timeout: float = 0.2, r: RDep = None):  # type: ignore[assignment]
    res = await r.blpop(keys, timeout=timeout)
    if res is None:
        return {"timeout": True}
    return {"key": res[0], "value": res[1]}


@lists.post("/brpop")
async def list_brpop(keys: Annotated[list[str], Query()], timeout: float = 0.2, r: RDep = None):  # type: ignore[assignment]
    res = await r.brpop(keys, timeout=timeout)
    if res is None:
        return {"timeout": True}
    return {"key": res[0], "value": res[1]}


@lists.post("/lpushx")
async def list_lpushx(k: str, v: str, r: RDep):
    return {"length": await r.lpushx(k, v)}


@lists.post("/rpushx")
async def list_rpushx(k: str, v: str, r: RDep):
    return {"length": await r.rpushx(k, v)}


@lists.post("/cancel_blpop")
async def list_cancel_blpop(k: str = "nonexistent_ch", timeout: float = 1.0):
    """Launch blpop in a task, cancel shortly after. Tests async cancellation."""
    r = aioredis.Redis(connection_pool=_pool())
    task = asyncio.create_task(r.blpop([k], timeout=timeout))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
        return {"cancelled": False}
    except asyncio.CancelledError:
        return {"cancelled": True}
    except Exception as e:
        return {"cancelled": False, "error": type(e).__name__}


app.include_router(lists)


# =====================================================================
# HASHES
# =====================================================================
hashes = APIRouter(prefix="/hash", tags=["hashes"])


@hashes.post("/hset")
async def hash_hset(k: str, mapping: Annotated[dict[str, str], Body()], r: RDep):
    return {"added": await r.hset(k, mapping=mapping)}


@hashes.get("/hget")
async def hash_hget(k: str, f: str, r: RDep):
    return {"value": await r.hget(k, f)}


@hashes.post("/hmset")
async def hash_hmset(k: str, mapping: Annotated[dict[str, str], Body()], r: RDep):
    await r.hmset(k, mapping)
    return {"ok": True}


@hashes.get("/hmget")
async def hash_hmget(k: str, fields: Annotated[list[str], Query(alias="f")], r: RDep):
    return {"values": await r.hmget(k, fields)}


@hashes.post("/hdel")
async def hash_hdel(k: str, fields: Annotated[list[str], Query(alias="f")], r: RDep):
    return {"deleted": await r.hdel(k, *fields)}


@hashes.get("/hexists")
async def hash_hexists(k: str, f: str, r: RDep):
    return {"exists": bool(await r.hexists(k, f))}


@hashes.get("/hkeys")
async def hash_hkeys(k: str, r: RDep):
    return {"keys": sorted(await r.hkeys(k))}


@hashes.get("/hvals")
async def hash_hvals(k: str, r: RDep):
    return {"values": sorted(await r.hvals(k))}


@hashes.get("/hgetall")
async def hash_hgetall(k: str, r: RDep):
    return {"map": await r.hgetall(k)}


@hashes.get("/hlen")
async def hash_hlen(k: str, r: RDep):
    return {"length": await r.hlen(k)}


@hashes.get("/hstrlen")
async def hash_hstrlen(k: str, f: str, r: RDep):
    return {"length": await r.hstrlen(k, f)}


@hashes.post("/hincrby")
async def hash_hincrby(k: str, f: str, n: int, r: RDep):
    return {"value": await r.hincrby(k, f, n)}


@hashes.post("/hincrbyfloat")
async def hash_hincrbyfloat(k: str, f: str, n: float, r: RDep):
    return {"value": float(await r.hincrbyfloat(k, f, n))}


@hashes.get("/hscan")
async def hash_hscan(k: str, match: str = "*", count: int = 100, r: RDep = None):  # type: ignore[assignment]
    cur = 0
    collected: dict[str, str] = {}
    while True:
        cur, batch = await r.hscan(k, cursor=cur, match=match, count=count)
        collected.update(batch)
        if cur == 0:
            break
    return {"map": dict(sorted(collected.items()))}


@hashes.post("/hsetnx")
async def hash_hsetnx(k: str, f: str, v: str, r: RDep):
    return {"created": bool(await r.hsetnx(k, f, v))}


app.include_router(hashes)


# =====================================================================
# SETS
# =====================================================================
sets = APIRouter(prefix="/set", tags=["sets"])


@sets.post("/sadd")
async def set_sadd(k: str, members: Annotated[list[str], Query(alias="m")], r: RDep):
    return {"added": await r.sadd(k, *members)}


@sets.post("/srem")
async def set_srem(k: str, members: Annotated[list[str], Query(alias="m")], r: RDep):
    return {"removed": await r.srem(k, *members)}


@sets.get("/smembers")
async def set_smembers(k: str, r: RDep):
    return {"members": sorted(await r.smembers(k))}


@sets.get("/scard")
async def set_scard(k: str, r: RDep):
    return {"count": await r.scard(k)}


@sets.get("/sismember")
async def set_sismember(k: str, m: str, r: RDep):
    return {"ismember": bool(await r.sismember(k, m))}


@sets.get("/smismember")
async def set_smismember(k: str, members: Annotated[list[str], Query(alias="m")], r: RDep):
    res = await r.smismember(k, members)
    return {"flags": [bool(x) for x in res]}


@sets.get("/sunion")
async def set_sunion(keys: Annotated[list[str], Query()], r: RDep):
    return {"members": sorted(await r.sunion(keys))}


@sets.get("/sinter")
async def set_sinter(keys: Annotated[list[str], Query()], r: RDep):
    return {"members": sorted(await r.sinter(keys))}


@sets.get("/sdiff")
async def set_sdiff(keys: Annotated[list[str], Query()], r: RDep):
    return {"members": sorted(await r.sdiff(keys))}


@sets.post("/sunionstore")
async def set_sunionstore(dst: str, keys: Annotated[list[str], Query()], r: RDep):
    return {"count": await r.sunionstore(dst, keys)}


@sets.post("/sinterstore")
async def set_sinterstore(dst: str, keys: Annotated[list[str], Query()], r: RDep):
    return {"count": await r.sinterstore(dst, keys)}


@sets.post("/sdiffstore")
async def set_sdiffstore(dst: str, keys: Annotated[list[str], Query()], r: RDep):
    return {"count": await r.sdiffstore(dst, keys)}


@sets.post("/spop")
async def set_spop(k: str, count: Optional[int] = None, r: RDep = None):  # type: ignore[assignment]
    v = await (r.spop(k, count) if count is not None else r.spop(k))
    if isinstance(v, (list, set)):
        return {"popped": sorted(v)}
    return {"popped": v}


@sets.get("/srandmember")
async def set_srandmember(k: str, count: Optional[int] = None, r: RDep = None):  # type: ignore[assignment]
    v = await (r.srandmember(k, count) if count is not None else r.srandmember(k))
    if isinstance(v, list):
        return {"n": len(v)}
    return {"present": v is not None}


@sets.post("/smove")
async def set_smove(src: str, dst: str, m: str, r: RDep):
    return {"moved": bool(await r.smove(src, dst, m))}


@sets.get("/sscan")
async def set_sscan(k: str, match: str = "*", r: RDep = None):  # type: ignore[assignment]
    cur = 0
    found: set[str] = set()
    while True:
        cur, batch = await r.sscan(k, cursor=cur, match=match)
        found.update(batch)
        if cur == 0:
            break
    return {"members": sorted(found)}


app.include_router(sets)


# =====================================================================
# SORTED SETS
# =====================================================================
zsets = APIRouter(prefix="/zset", tags=["zsets"])


class ZAddBody(BaseModel):
    mapping: dict[str, float]
    nx: bool = False
    xx: bool = False
    ch: bool = False
    incr: bool = False
    gt: bool = False
    lt: bool = False


@zsets.post("/zadd")
async def zset_zadd(k: str, body: ZAddBody, r: RDep):
    res = await r.zadd(
        k, body.mapping,
        nx=body.nx, xx=body.xx, ch=body.ch, incr=body.incr,
        gt=body.gt, lt=body.lt,
    )
    return {"result": res}


@zsets.get("/zrange")
async def zset_zrange(k: str, start: int = 0, stop: int = -1, withscores: bool = False, r: RDep = None):  # type: ignore[assignment]
    res = await r.zrange(k, start, stop, withscores=withscores)
    if withscores:
        return {"items": [[m, float(s)] for m, s in res]}
    return {"items": res}


@zsets.get("/zrevrange")
async def zset_zrevrange(k: str, start: int = 0, stop: int = -1, withscores: bool = False, r: RDep = None):  # type: ignore[assignment]
    res = await r.zrevrange(k, start, stop, withscores=withscores)
    if withscores:
        return {"items": [[m, float(s)] for m, s in res]}
    return {"items": res}


@zsets.get("/zrangebyscore")
async def zset_zrangebyscore(k: str, min: str, max: str, r: RDep):
    return {"items": await r.zrangebyscore(k, min, max)}


@zsets.get("/zrangebylex")
async def zset_zrangebylex(k: str, min: str, max: str, r: RDep):
    return {"items": await r.zrangebylex(k, min, max)}


@zsets.get("/zscore")
async def zset_zscore(k: str, m: str, r: RDep):
    s = await r.zscore(k, m)
    return {"score": float(s) if s is not None else None}


@zsets.get("/zmscore")
async def zset_zmscore(k: str, members: Annotated[list[str], Query(alias="m")], r: RDep):
    raw = await r.zmscore(k, members)
    return {"scores": [None if x is None else float(x) for x in raw]}


@zsets.post("/zincrby")
async def zset_zincrby(k: str, m: str, by: float, r: RDep):
    return {"score": float(await r.zincrby(k, by, m))}


@zsets.get("/zrank")
async def zset_zrank(k: str, m: str, r: RDep):
    return {"rank": await r.zrank(k, m)}


@zsets.get("/zrevrank")
async def zset_zrevrank(k: str, m: str, r: RDep):
    return {"rank": await r.zrevrank(k, m)}


@zsets.get("/zcard")
async def zset_zcard(k: str, r: RDep):
    return {"count": await r.zcard(k)}


@zsets.get("/zcount")
async def zset_zcount(k: str, min: str, max: str, r: RDep):
    return {"count": await r.zcount(k, min, max)}


@zsets.get("/zlexcount")
async def zset_zlexcount(k: str, min: str, max: str, r: RDep):
    return {"count": await r.zlexcount(k, min, max)}


@zsets.post("/zpopmin")
async def zset_zpopmin(k: str, count: int = 1, r: RDep = None):  # type: ignore[assignment]
    res = await r.zpopmin(k, count)
    return {"items": [[m, float(s)] for m, s in res]}


@zsets.post("/zpopmax")
async def zset_zpopmax(k: str, count: int = 1, r: RDep = None):  # type: ignore[assignment]
    res = await r.zpopmax(k, count)
    return {"items": [[m, float(s)] for m, s in res]}


@zsets.post("/bzpopmin")
async def zset_bzpopmin(keys: Annotated[list[str], Query()], timeout: float = 0.2, r: RDep = None):  # type: ignore[assignment]
    res = await r.bzpopmin(keys, timeout=timeout)
    if res is None:
        return {"timeout": True}
    return {"key": res[0], "member": res[1], "score": float(res[2])}


@zsets.post("/bzpopmax")
async def zset_bzpopmax(keys: Annotated[list[str], Query()], timeout: float = 0.2, r: RDep = None):  # type: ignore[assignment]
    res = await r.bzpopmax(keys, timeout=timeout)
    if res is None:
        return {"timeout": True}
    return {"key": res[0], "member": res[1], "score": float(res[2])}


@zsets.post("/zrangestore")
async def zset_zrangestore(dst: str, src: str, start: int = 0, stop: int = -1, r: RDep = None):  # type: ignore[assignment]
    return {"stored": await r.zrangestore(dst, src, start, stop)}


@zsets.post("/zunionstore")
async def zset_zunionstore(dst: str, keys: Annotated[list[str], Query()], r: RDep):
    return {"stored": await r.zunionstore(dst, keys)}


@zsets.post("/zinterstore")
async def zset_zinterstore(dst: str, keys: Annotated[list[str], Query()], r: RDep):
    return {"stored": await r.zinterstore(dst, keys)}


@zsets.post("/zdiffstore")
async def zset_zdiffstore(dst: str, keys: Annotated[list[str], Query()], r: RDep):
    return {"stored": await r.zdiffstore(dst, keys)}


@zsets.get("/zscan")
async def zset_zscan(k: str, match: str = "*", r: RDep = None):  # type: ignore[assignment]
    cur = 0
    pairs: list[tuple[str, float]] = []
    while True:
        cur, batch = await r.zscan(k, cursor=cur, match=match)
        pairs.extend([(m, float(s)) for m, s in batch])
        if cur == 0:
            break
    pairs.sort(key=lambda x: (x[1], x[0]))
    return {"items": [[m, s] for m, s in pairs]}


@zsets.post("/zadd_inf")
async def zset_zadd_inf(k: str, r: RDep):
    await r.zadd(k, {"neg_inf": float("-inf"), "pos_inf": float("inf"), "zero": 0.0})
    inf = await r.zscore(k, "pos_inf")
    ninf = await r.zscore(k, "neg_inf")
    return {"pos_is_inf": inf == float("inf"), "neg_is_ninf": ninf == float("-inf")}


app.include_router(zsets)


# =====================================================================
# PUB/SUB (async)
# =====================================================================
pubsub_r = APIRouter(prefix="/pubsub", tags=["pubsub"])


_SUB_STATE: dict[str, dict] = {}
_SUB_TASKS: dict[str, asyncio.Task] = {}


async def _async_subscriber(sub_id: str, channels: list[str], patterns: list[str], duration: float):
    r = aioredis.Redis(connection_pool=_pool())
    ps = r.pubsub(ignore_subscribe_messages=True)
    if channels:
        await ps.subscribe(*channels)
    if patterns:
        await ps.psubscribe(*patterns)
    received: list[dict] = []
    deadline = time.time() + duration
    try:
        while time.time() < deadline:
            msg = await ps.get_message(timeout=0.05)
            if msg is None:
                continue
            received.append({
                "type": msg.get("type"),
                "channel": msg.get("channel"),
                "pattern": msg.get("pattern"),
                "data": msg.get("data"),
            })
    finally:
        try:
            await ps.close()
        except Exception:
            pass
    _SUB_STATE[sub_id]["messages"] = received
    _SUB_STATE[sub_id]["done"] = True


@pubsub_r.post("/subscribe_start")
async def pubsub_subscribe_start(
    sub_id: str,
    channels: Annotated[list[str], Query(alias="c")] = [],
    patterns: Annotated[list[str], Query(alias="p")] = [],
    duration_ms: int = 500,
):
    _SUB_STATE[sub_id] = {"messages": [], "done": False}
    task = asyncio.create_task(
        _async_subscriber(sub_id, channels, patterns, duration_ms / 1000.0)
    )
    _SUB_TASKS[sub_id] = task
    await asyncio.sleep(0.1)
    return {"started": True}


@pubsub_r.post("/publish")
async def pubsub_publish(channel: str, msg: str, r: RDep):
    return {"receivers": await r.publish(channel, msg)}


@pubsub_r.get("/subscribe_result")
async def pubsub_subscribe_result(sub_id: str, wait_ms: int = 600):
    deadline = time.time() + (wait_ms / 1000.0)
    while time.time() < deadline:
        state = _SUB_STATE.get(sub_id)
        if state and state.get("done"):
            msgs = sorted(state["messages"], key=lambda m: (m["channel"] or "", str(m["data"])))
            return {"count": len(msgs), "messages": msgs}
        await asyncio.sleep(0.02)
    state = _SUB_STATE.get(sub_id, {})
    msgs = sorted(state.get("messages", []), key=lambda m: (m["channel"] or "", str(m["data"])))
    return {"count": len(msgs), "messages": msgs, "incomplete": True}


@pubsub_r.post("/cancel_get_message")
async def pubsub_cancel_get_message():
    """Start a get_message with large timeout, then cancel it."""
    r = aioredis.Redis(connection_pool=_pool())
    ps = r.pubsub(ignore_subscribe_messages=True)
    await ps.subscribe("nobody-publishes-here")

    async def _await():
        return await ps.get_message(timeout=5.0)

    task = asyncio.create_task(_await())
    await asyncio.sleep(0.05)
    task.cancel()
    cancelled = False
    try:
        await task
    except asyncio.CancelledError:
        cancelled = True
    except Exception:
        pass
    try:
        await ps.close()
    except Exception:
        pass
    return {"cancelled": cancelled}


app.include_router(pubsub_r)


# =====================================================================
# PIPELINES (async)
# =====================================================================
pipes = APIRouter(prefix="/pipe", tags=["pipelines"])


@pipes.post("/simple")
async def pipe_simple(key_prefix: str, r: RDep):
    async with r.pipeline() as pipe:
        pipe.set(f"{key_prefix}:a", "1")
        pipe.set(f"{key_prefix}:b", "2")
        pipe.set(f"{key_prefix}:c", "3")
        pipe.incr(f"{key_prefix}:counter")
        pipe.incr(f"{key_prefix}:counter")
        res = await pipe.execute()
    return {"results": [str(x) for x in res]}


@pipes.post("/transaction")
async def pipe_transaction(key: str, r: RDep):
    async with r.pipeline(transaction=True) as pipe:
        pipe.set(key, "initial")
        pipe.incr(f"{key}:hits")
        pipe.incr(f"{key}:hits")
        pipe.get(key)
        res = await pipe.execute()
    return {"results": [x.decode() if isinstance(x, bytes) else x for x in res]}


@pipes.post("/watch")
async def pipe_watch(key: str, r: RDep):
    await r.set(key, "100")
    async with r.pipeline() as pipe:
        try:
            await pipe.watch(key)
            current_raw = await pipe.get(key)
            current = int(current_raw)
            pipe.multi()
            pipe.set(key, str(current + 1))
            await pipe.execute()
            return {"ok": True, "final": await r.get(key)}
        except redis.WatchError:
            return {"ok": False}


@pipes.post("/mixed")
async def pipe_mixed(ns: str, r: RDep):
    async with r.pipeline() as pipe:
        pipe.lpush(f"{ns}:list", "x", "y", "z")
        pipe.sadd(f"{ns}:set", "a", "b", "c")
        pipe.hset(f"{ns}:hash", mapping={"f1": "v1", "f2": "v2"})
        pipe.zadd(f"{ns}:zset", {"m1": 1.0, "m2": 2.0})
        res = await pipe.execute()
    return {"ops": len(res), "values": [int(x) if isinstance(x, int) else str(x) for x in res]}


app.include_router(pipes)


# =====================================================================
# STREAMS (async)
# =====================================================================
streams_r = APIRouter(prefix="/stream", tags=["streams"])


@streams_r.post("/xadd")
async def stream_xadd(k: str, fields: Annotated[dict[str, str], Body()],
                      maxlen: Optional[int] = None,
                      nomkstream: bool = False, r: RDep = None):  # type: ignore[assignment]
    try:
        msg_id = await r.xadd(k, fields, maxlen=maxlen, nomkstream=nomkstream)
        return {"id": msg_id}
    except redis.ResponseError as e:
        raise HTTPException(status_code=400, detail=str(e))


@streams_r.get("/xlen")
async def stream_xlen(k: str, r: RDep):
    return {"length": await r.xlen(k)}


@streams_r.get("/xrange")
async def stream_xrange(k: str, r: RDep):
    items = await r.xrange(k, min="-", max="+")
    return {"items": [[mid, fields] for mid, fields in items]}


@streams_r.get("/xrevrange")
async def stream_xrevrange(k: str, r: RDep):
    items = await r.xrevrange(k, max="+", min="-")
    return {"items": [[mid, fields] for mid, fields in items]}


@streams_r.get("/xread")
async def stream_xread(k: str, start: str = "0", count: int = 100, r: RDep = None):  # type: ignore[assignment]
    res = await r.xread({k: start}, count=count)
    norm = []
    for stream_name, entries in res or []:
        for mid, fields in entries:
            norm.append([stream_name, mid, fields])
    return {"entries": norm}


@streams_r.post("/xgroup_create")
async def stream_xgroup_create(k: str, group: str, id: str = "$", mkstream: bool = True, r: RDep = None):  # type: ignore[assignment]
    try:
        await r.xgroup_create(k, group, id=id, mkstream=mkstream)
        return {"created": True}
    except redis.ResponseError as e:
        return {"created": False, "error": str(e)}


@streams_r.post("/xreadgroup")
async def stream_xreadgroup(k: str, group: str, consumer: str, count: int = 10, r: RDep = None):  # type: ignore[assignment]
    res = await r.xreadgroup(group, consumer, {k: ">"}, count=count)
    norm = []
    for stream_name, entries in res or []:
        for mid, fields in entries:
            norm.append([stream_name, mid, fields])
    return {"entries": norm}


@streams_r.post("/xack")
async def stream_xack(k: str, group: str, ids: Annotated[list[str], Query()], r: RDep):
    return {"acked": await r.xack(k, group, *ids)}


@streams_r.get("/xpending")
async def stream_xpending(k: str, group: str, r: RDep):
    p = await r.xpending(k, group)
    return {"pending": p.get("pending") if isinstance(p, dict) else p[0]}


@streams_r.post("/xtrim")
async def stream_xtrim(k: str, maxlen: int, r: RDep):
    return {"trimmed": await r.xtrim(k, maxlen=maxlen)}


@streams_r.post("/xdel")
async def stream_xdel(k: str, ids: Annotated[list[str], Query()], r: RDep):
    return {"deleted": await r.xdel(k, *ids)}


app.include_router(streams_r)


# =====================================================================
# LUA (async)
# =====================================================================
lua = APIRouter(prefix="/lua", tags=["scripting"])


@lua.post("/eval")
async def lua_eval(body: Annotated[dict, Body()], r: RDep):
    script = body.get("script", "")
    keys = body.get("keys", [])
    args = body.get("args", [])
    res = await r.eval(script, len(keys), *keys, *args)
    if isinstance(res, bytes):
        res = res.decode()
    return {"result": res}


@lua.post("/load_and_evalsha")
async def lua_load_and_evalsha(body: Annotated[dict, Body()], r: RDep):
    script = body.get("script", "")
    keys = body.get("keys", [])
    args = body.get("args", [])
    sha = await r.script_load(script)
    res = await r.evalsha(sha, len(keys), *keys, *args)
    if isinstance(res, bytes):
        res = res.decode()
    return {"sha_len": len(sha), "result": res}


@lua.post("/incr_if_eq")
async def lua_incr_if_eq(k: str, expected: str, r: RDep):
    script = """
    local v = redis.call('GET', KEYS[1])
    if v == ARGV[1] then
      return redis.call('INCR', KEYS[1])
    else
      return -1
    end
    """
    return {"result": await r.eval(script, 1, k, expected)}


app.include_router(lua)


# =====================================================================
# HIGH-LEVEL APP PATTERNS (async)
# =====================================================================
patterns = APIRouter(prefix="/app", tags=["patterns"])


async def async_cache_get_or_set(r: aioredis.Redis, key: str, ttl: int, producer):
    hit = await r.get(key)
    if hit is not None:
        return {"cached": True, "value": hit}
    v = producer()
    await r.setex(key, ttl, v)
    return {"cached": False, "value": v}


@patterns.get("/cache/hello")
async def cache_hello(name: str, r: RDep):
    key = f"cache:hello:{name}"
    return await async_cache_get_or_set(r, key, 60, lambda: f"hello, {name}")


@patterns.post("/ratelimit")
async def ratelimit(user: str, limit: int = 3, window: int = 60, r: RDep = None):  # type: ignore[assignment]
    key = f"rl:{user}"
    n = await r.incr(key)
    if n == 1:
        await r.expire(key, window)
    if n > limit:
        raise HTTPException(status_code=429, detail="rate limited")
    return {"count": n, "limit": limit}


@patterns.post("/session/create")
async def session_create(user: str, r: RDep):
    sid = f"sess:{user}:{int(time.time()*1000) % 100000}"
    await r.setex(sid, 300, json.dumps({"user": user, "ctime": 0}))
    return {"session_id": sid}


@patterns.get("/session/get")
async def session_get(sid: str, r: RDep):
    v = await r.get(sid)
    if v is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"session": json.loads(v)}


@patterns.post("/flags/set")
async def flags_set(mapping: Annotated[dict[str, str], Body()], r: RDep):
    await r.hset("flags", mapping=mapping)
    return {"ok": True, "count": len(mapping)}


@patterns.get("/flags/all")
async def flags_all(r: RDep):
    return {"flags": await r.hgetall("flags")}


@patterns.get("/flags/one")
async def flags_one(name: str, r: RDep):
    v = await r.hget("flags", name)
    return {"value": v, "enabled": v == "on"}


@patterns.post("/lb/add")
async def lb_add(user: str, score: float, r: RDep):
    await r.zadd("leaderboard", {user: score})
    return {"ok": True, "rank": await r.zrevrank("leaderboard", user)}


@patterns.get("/lb/top")
async def lb_top(n: int = 5, r: RDep = None):  # type: ignore[assignment]
    items = await r.zrevrange("leaderboard", 0, n - 1, withscores=True)
    return {"top": [[m, float(s)] for m, s in items]}


@patterns.post("/lock/acquire")
async def lock_acquire(name: str, ttl_ms: int = 2000, r: RDep = None):  # type: ignore[assignment]
    token = f"tok-{int(time.time()*1000) % 1000000}"
    ok = await r.set(f"lock:{name}", token, nx=True, px=ttl_ms)
    return {"acquired": bool(ok), "token": token if ok else None}


@patterns.post("/lock/release")
async def lock_release(name: str, token: str, r: RDep):
    script = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('DEL', KEYS[1])
    else
      return 0
    end
    """
    return {"released": bool(await r.eval(script, 1, f"lock:{name}", token))}


app.include_router(patterns)


# =====================================================================
# SERIALIZATION (async)
# =====================================================================
serial = APIRouter(prefix="/ser", tags=["serialization"])


@serial.post("/utf8")
async def ser_utf8(k: str, v: str, r: RDep):
    await r.set(k, v)
    return {"value": await r.get(k)}


@serial.post("/unicode")
async def ser_unicode(k: str, r: RDep):
    s = "héllo 🌍 こんにちは ∞"
    await r.set(k, s)
    return {"value": await r.get(k), "len_chars": len(s)}


@serial.post("/json")
async def ser_json(k: str, payload: Annotated[dict, Body()], r: RDep):
    await r.set(k, json.dumps(payload, sort_keys=True))
    return {"value": json.loads(await r.get(k))}


@serial.post("/bytes")
async def ser_bytes(k: str, v: str):
    raw = get_raw()
    await raw.set(k, v.encode())
    got = await raw.get(k)
    return {"length": len(got or b""), "equal": got == v.encode()}


app.include_router(serial)


# =====================================================================
# ERROR HANDLING (async)
# =====================================================================
errs = APIRouter(prefix="/err", tags=["errors"])


@errs.get("/wrongtype")
async def err_wrongtype(k: str, r: RDep):
    await r.set(k, "not-a-list")
    try:
        await r.lpush(k, "x")
        return {"error": None}
    except redis.ResponseError as e:
        raise HTTPException(status_code=409, detail=str(e).split()[0])


@errs.get("/nonexistent_get")
async def err_nonexistent_get(r: RDep):
    return {"value": await r.get("definitely-missing-key-xyz")}


@errs.get("/type_nonexistent")
async def err_type_nonexistent(r: RDep):
    return {"type": await r.type("definitely-missing-key-xyz")}


@errs.get("/ttl_nonexistent")
async def err_ttl_nonexistent(r: RDep):
    return {"ttl": await r.ttl("definitely-missing-key-xyz")}


app.include_router(errs)


# =====================================================================
# ADMIN (async)
# =====================================================================
admin = APIRouter(prefix="/_admin", tags=["admin"])


@admin.post("/flushdb")
async def admin_flushdb(r: RDep):
    await r.flushdb()
    return {"ok": True}


@admin.get("/dbsize")
async def admin_dbsize(r: RDep):
    return {"size": await r.dbsize()}


app.include_router(admin)
