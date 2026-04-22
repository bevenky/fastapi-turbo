"""Redis integration parity app (SYNC redis-py).

Exposes ~150 endpoints that each exercise a Redis command/pattern. Both stock
FastAPI and fastapi-rs mount this same app, and the runner compares each
endpoint's full HTTP response and the resulting Redis state.

Connects to Redis at 127.0.0.1:<port-from-env> or 6392 by default.

Each endpoint uses a short, deterministic namespace. The runner FLUSHes the
Redis DB between every test for strict isolation.

Uses ONLY public FastAPI imports. fastapi-rs's compat shim maps them.
"""
from __future__ import annotations

import json
import os
import time
import threading
from typing import Annotated, Any, Optional

from fastapi import (
    FastAPI,
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Body,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

import redis


REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6392"))
REDIS_DB = int(os.environ.get("REDIS_DB", "0"))


# ── Clients: one decoded, one raw ──────────────────────────────────
# We build a shared sync connection pool so endpoints and Depends share it.
_POOL = redis.ConnectionPool(
    host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
    decode_responses=True, max_connections=128,
)
_RAW_POOL = redis.ConnectionPool(
    host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
    decode_responses=False, max_connections=64,
)


def get_redis() -> redis.Redis:
    """Dependency yielding a decoded Redis client from the pool."""
    r = redis.Redis(connection_pool=_POOL)
    try:
        yield r
    finally:
        # Connection returns to pool automatically.
        pass


def get_raw() -> redis.Redis:
    return redis.Redis(connection_pool=_RAW_POOL)


RDep = Annotated[redis.Redis, Depends(get_redis)]


app = FastAPI(title="Redis Parity App (sync)", version="1.0.0")


@app.get("/health")
def health():
    try:
        redis.Redis(connection_pool=_POOL).ping()
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
def str_set(k: str, body: SetBody, r: RDep):
    ok = r.set(k, body.value, ex=body.ex, nx=body.nx or None, xx=body.xx or None)
    return {"ok": bool(ok)}


@strings.get("/get")
def str_get(k: str, r: RDep):
    return {"value": r.get(k)}


@strings.post("/getset")
def str_getset(k: str, v: str, r: RDep):
    return {"prev": r.getset(k, v)}


@strings.post("/setex")
def str_setex(k: str, seconds: int, v: str, r: RDep):
    r.setex(k, seconds, v)
    return {"ok": True, "ttl": r.ttl(k)}


@strings.post("/psetex")
def str_psetex(k: str, ms: int, v: str, r: RDep):
    r.psetex(k, ms, v)
    return {"ok": True, "pttl_gt0": (r.pttl(k) or 0) > 0}


@strings.post("/setnx")
def str_setnx(k: str, v: str, r: RDep):
    return {"created": bool(r.setnx(k, v))}


@strings.post("/mset")
def str_mset(mapping: Annotated[dict[str, str], Body()], r: RDep):
    r.mset(mapping)
    return {"ok": True, "count": len(mapping)}


@strings.get("/mget")
def str_mget(keys: Annotated[list[str], Query()], r: RDep):
    return {"values": r.mget(keys)}


@strings.get("/getrange")
def str_getrange(k: str, start: int, end: int, r: RDep):
    return {"value": r.getrange(k, start, end)}


@strings.post("/setrange")
def str_setrange(k: str, offset: int, v: str, r: RDep):
    return {"new_length": r.setrange(k, offset, v)}


@strings.get("/strlen")
def str_strlen(k: str, r: RDep):
    return {"length": r.strlen(k)}


@strings.post("/incr")
def str_incr(k: str, r: RDep):
    return {"value": r.incr(k)}


@strings.post("/decr")
def str_decr(k: str, r: RDep):
    return {"value": r.decr(k)}


@strings.post("/incrby")
def str_incrby(k: str, n: int, r: RDep):
    return {"value": r.incrby(k, n)}


@strings.post("/decrby")
def str_decrby(k: str, n: int, r: RDep):
    return {"value": r.decrby(k, n)}


@strings.post("/incrbyfloat")
def str_incrbyfloat(k: str, n: float, r: RDep):
    return {"value": float(r.incrbyfloat(k, n))}


@strings.post("/append")
def str_append(k: str, v: str, r: RDep):
    return {"new_length": r.append(k, v)}


@strings.post("/expire")
def str_expire(k: str, seconds: int, r: RDep):
    return {"applied": bool(r.expire(k, seconds))}


@strings.post("/expireat")
def str_expireat(k: str, ts: int, r: RDep):
    return {"applied": bool(r.expireat(k, ts))}


@strings.post("/persist")
def str_persist(k: str, r: RDep):
    return {"applied": bool(r.persist(k))}


@strings.get("/ttl")
def str_ttl(k: str, r: RDep):
    return {"ttl": r.ttl(k)}


@strings.get("/pttl")
def str_pttl(k: str, r: RDep):
    p = r.pttl(k)
    # Normalize: real pttl can drift (ms); just return bucket.
    if p is None or p < 0:
        return {"pttl_bucket": p}
    return {"pttl_bucket": "positive"}


@strings.get("/type")
def str_type(k: str, r: RDep):
    return {"type": r.type(k)}


@strings.get("/exists")
def str_exists(keys: Annotated[list[str], Query()], r: RDep):
    return {"count": r.exists(*keys)}


@strings.post("/del")
def str_del(keys: Annotated[list[str], Query()], r: RDep):
    return {"deleted": r.delete(*keys)}


@strings.post("/unlink")
def str_unlink(keys: Annotated[list[str], Query()], r: RDep):
    return {"unlinked": r.unlink(*keys)}


@strings.get("/keys")
def str_keys(pattern: str, r: RDep):
    return {"keys": sorted(r.keys(pattern))}


@strings.get("/scan")
def str_scan(match: str = "*", count: int = 100, r: RDep = None):  # type: ignore[assignment]
    cur = 0
    found: list[str] = []
    while True:
        cur, batch = r.scan(cursor=cur, match=match, count=count)
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
def list_lpush(k: str, vals: Annotated[list[str], Query(alias="v")], r: RDep):
    return {"length": r.lpush(k, *vals)}


@lists.post("/rpush")
def list_rpush(k: str, vals: Annotated[list[str], Query(alias="v")], r: RDep):
    return {"length": r.rpush(k, *vals)}


@lists.post("/lpop")
def list_lpop(k: str, count: Optional[int] = None, r: RDep = None):  # type: ignore[assignment]
    return {"value": r.lpop(k, count) if count else r.lpop(k)}


@lists.post("/rpop")
def list_rpop(k: str, count: Optional[int] = None, r: RDep = None):  # type: ignore[assignment]
    return {"value": r.rpop(k, count) if count else r.rpop(k)}


@lists.get("/lrange")
def list_lrange(k: str, start: int = 0, stop: int = -1, r: RDep = None):  # type: ignore[assignment]
    return {"values": r.lrange(k, start, stop)}


@lists.get("/llen")
def list_llen(k: str, r: RDep):
    return {"length": r.llen(k)}


@lists.get("/lindex")
def list_lindex(k: str, i: int, r: RDep):
    return {"value": r.lindex(k, i)}


@lists.post("/lset")
def list_lset(k: str, i: int, v: str, r: RDep):
    try:
        r.lset(k, i, v)
        return {"ok": True}
    except redis.ResponseError as e:
        raise HTTPException(status_code=400, detail=str(e))


@lists.post("/linsert")
def list_linsert(k: str, where: str, pivot: str, v: str, r: RDep):
    return {"length": r.linsert(k, where.upper(), pivot, v)}


@lists.post("/lrem")
def list_lrem(k: str, count: int, v: str, r: RDep):
    return {"removed": r.lrem(k, count, v)}


@lists.post("/ltrim")
def list_ltrim(k: str, start: int, stop: int, r: RDep):
    r.ltrim(k, start, stop)
    return {"ok": True}


@lists.post("/rpoplpush")
def list_rpoplpush(src: str, dst: str, r: RDep):
    return {"value": r.rpoplpush(src, dst)}


@lists.post("/lmove")
def list_lmove(src: str, dst: str, where_from: str = "LEFT", where_to: str = "RIGHT", r: RDep = None):  # type: ignore[assignment]
    return {"value": r.lmove(src, dst, where_from.upper(), where_to.upper())}


@lists.post("/blpop")
def list_blpop(keys: Annotated[list[str], Query()], timeout: float = 0.2, r: RDep = None):  # type: ignore[assignment]
    res = r.blpop(keys, timeout=timeout)
    if res is None:
        return {"timeout": True}
    return {"key": res[0], "value": res[1]}


@lists.post("/brpop")
def list_brpop(keys: Annotated[list[str], Query()], timeout: float = 0.2, r: RDep = None):  # type: ignore[assignment]
    res = r.brpop(keys, timeout=timeout)
    if res is None:
        return {"timeout": True}
    return {"key": res[0], "value": res[1]}


@lists.post("/lpushx")
def list_lpushx(k: str, v: str, r: RDep):
    return {"length": r.lpushx(k, v)}


@lists.post("/rpushx")
def list_rpushx(k: str, v: str, r: RDep):
    return {"length": r.rpushx(k, v)}


app.include_router(lists)


# =====================================================================
# HASHES
# =====================================================================
hashes = APIRouter(prefix="/hash", tags=["hashes"])


@hashes.post("/hset")
def hash_hset(k: str, mapping: Annotated[dict[str, str], Body()], r: RDep):
    return {"added": r.hset(k, mapping=mapping)}


@hashes.get("/hget")
def hash_hget(k: str, f: str, r: RDep):
    return {"value": r.hget(k, f)}


@hashes.post("/hmset")
def hash_hmset(k: str, mapping: Annotated[dict[str, str], Body()], r: RDep):
    r.hmset(k, mapping)
    return {"ok": True}


@hashes.get("/hmget")
def hash_hmget(k: str, fields: Annotated[list[str], Query(alias="f")], r: RDep):
    return {"values": r.hmget(k, fields)}


@hashes.post("/hdel")
def hash_hdel(k: str, fields: Annotated[list[str], Query(alias="f")], r: RDep):
    return {"deleted": r.hdel(k, *fields)}


@hashes.get("/hexists")
def hash_hexists(k: str, f: str, r: RDep):
    return {"exists": bool(r.hexists(k, f))}


@hashes.get("/hkeys")
def hash_hkeys(k: str, r: RDep):
    return {"keys": sorted(r.hkeys(k))}


@hashes.get("/hvals")
def hash_hvals(k: str, r: RDep):
    return {"values": sorted(r.hvals(k))}


@hashes.get("/hgetall")
def hash_hgetall(k: str, r: RDep):
    return {"map": r.hgetall(k)}


@hashes.get("/hlen")
def hash_hlen(k: str, r: RDep):
    return {"length": r.hlen(k)}


@hashes.get("/hstrlen")
def hash_hstrlen(k: str, f: str, r: RDep):
    return {"length": r.hstrlen(k, f)}


@hashes.post("/hincrby")
def hash_hincrby(k: str, f: str, n: int, r: RDep):
    return {"value": r.hincrby(k, f, n)}


@hashes.post("/hincrbyfloat")
def hash_hincrbyfloat(k: str, f: str, n: float, r: RDep):
    return {"value": float(r.hincrbyfloat(k, f, n))}


@hashes.get("/hscan")
def hash_hscan(k: str, match: str = "*", count: int = 100, r: RDep = None):  # type: ignore[assignment]
    cur = 0
    collected: dict[str, str] = {}
    while True:
        cur, batch = r.hscan(k, cursor=cur, match=match, count=count)
        collected.update(batch)
        if cur == 0:
            break
    return {"map": dict(sorted(collected.items()))}


@hashes.post("/hsetnx")
def hash_hsetnx(k: str, f: str, v: str, r: RDep):
    return {"created": bool(r.hsetnx(k, f, v))}


app.include_router(hashes)


# =====================================================================
# SETS
# =====================================================================
sets = APIRouter(prefix="/set", tags=["sets"])


@sets.post("/sadd")
def set_sadd(k: str, members: Annotated[list[str], Query(alias="m")], r: RDep):
    return {"added": r.sadd(k, *members)}


@sets.post("/srem")
def set_srem(k: str, members: Annotated[list[str], Query(alias="m")], r: RDep):
    return {"removed": r.srem(k, *members)}


@sets.get("/smembers")
def set_smembers(k: str, r: RDep):
    return {"members": sorted(r.smembers(k))}


@sets.get("/scard")
def set_scard(k: str, r: RDep):
    return {"count": r.scard(k)}


@sets.get("/sismember")
def set_sismember(k: str, m: str, r: RDep):
    return {"ismember": bool(r.sismember(k, m))}


@sets.get("/smismember")
def set_smismember(k: str, members: Annotated[list[str], Query(alias="m")], r: RDep):
    res = r.smismember(k, members)
    return {"flags": [bool(x) for x in res]}


@sets.get("/sunion")
def set_sunion(keys: Annotated[list[str], Query()], r: RDep):
    return {"members": sorted(r.sunion(keys))}


@sets.get("/sinter")
def set_sinter(keys: Annotated[list[str], Query()], r: RDep):
    return {"members": sorted(r.sinter(keys))}


@sets.get("/sdiff")
def set_sdiff(keys: Annotated[list[str], Query()], r: RDep):
    return {"members": sorted(r.sdiff(keys))}


@sets.post("/sunionstore")
def set_sunionstore(dst: str, keys: Annotated[list[str], Query()], r: RDep):
    return {"count": r.sunionstore(dst, keys)}


@sets.post("/sinterstore")
def set_sinterstore(dst: str, keys: Annotated[list[str], Query()], r: RDep):
    return {"count": r.sinterstore(dst, keys)}


@sets.post("/sdiffstore")
def set_sdiffstore(dst: str, keys: Annotated[list[str], Query()], r: RDep):
    return {"count": r.sdiffstore(dst, keys)}


@sets.post("/spop")
def set_spop(k: str, count: Optional[int] = None, r: RDep = None):  # type: ignore[assignment]
    v = r.spop(k, count) if count is not None else r.spop(k)
    # For parity, assume test caller pre-seeds single-member set; we return sorted
    if isinstance(v, (list, set)):
        return {"popped": sorted(v)}
    return {"popped": v}


@sets.get("/srandmember")
def set_srandmember(k: str, count: Optional[int] = None, r: RDep = None):  # type: ignore[assignment]
    v = r.srandmember(k, count) if count is not None else r.srandmember(k)
    if isinstance(v, list):
        return {"n": len(v)}  # value is random; return count only
    return {"present": v is not None}


@sets.post("/smove")
def set_smove(src: str, dst: str, m: str, r: RDep):
    return {"moved": bool(r.smove(src, dst, m))}


@sets.get("/sscan")
def set_sscan(k: str, match: str = "*", r: RDep = None):  # type: ignore[assignment]
    cur = 0
    found: set[str] = set()
    while True:
        cur, batch = r.sscan(k, cursor=cur, match=match)
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
def zset_zadd(k: str, body: ZAddBody, r: RDep):
    res = r.zadd(
        k, body.mapping,
        nx=body.nx, xx=body.xx, ch=body.ch, incr=body.incr,
        gt=body.gt, lt=body.lt,
    )
    return {"result": res}


@zsets.get("/zrange")
def zset_zrange(k: str, start: int = 0, stop: int = -1, withscores: bool = False, r: RDep = None):  # type: ignore[assignment]
    res = r.zrange(k, start, stop, withscores=withscores)
    if withscores:
        return {"items": [[m, float(s)] for m, s in res]}
    return {"items": res}


@zsets.get("/zrevrange")
def zset_zrevrange(k: str, start: int = 0, stop: int = -1, withscores: bool = False, r: RDep = None):  # type: ignore[assignment]
    res = r.zrevrange(k, start, stop, withscores=withscores)
    if withscores:
        return {"items": [[m, float(s)] for m, s in res]}
    return {"items": res}


@zsets.get("/zrangebyscore")
def zset_zrangebyscore(k: str, min: str, max: str, r: RDep):
    return {"items": r.zrangebyscore(k, min, max)}


@zsets.get("/zrangebylex")
def zset_zrangebylex(k: str, min: str, max: str, r: RDep):
    return {"items": r.zrangebylex(k, min, max)}


@zsets.get("/zscore")
def zset_zscore(k: str, m: str, r: RDep):
    s = r.zscore(k, m)
    return {"score": float(s) if s is not None else None}


@zsets.get("/zmscore")
def zset_zmscore(k: str, members: Annotated[list[str], Query(alias="m")], r: RDep):
    raw = r.zmscore(k, members)
    return {"scores": [None if x is None else float(x) for x in raw]}


@zsets.post("/zincrby")
def zset_zincrby(k: str, m: str, by: float, r: RDep):
    return {"score": float(r.zincrby(k, by, m))}


@zsets.get("/zrank")
def zset_zrank(k: str, m: str, r: RDep):
    return {"rank": r.zrank(k, m)}


@zsets.get("/zrevrank")
def zset_zrevrank(k: str, m: str, r: RDep):
    return {"rank": r.zrevrank(k, m)}


@zsets.get("/zcard")
def zset_zcard(k: str, r: RDep):
    return {"count": r.zcard(k)}


@zsets.get("/zcount")
def zset_zcount(k: str, min: str, max: str, r: RDep):
    return {"count": r.zcount(k, min, max)}


@zsets.get("/zlexcount")
def zset_zlexcount(k: str, min: str, max: str, r: RDep):
    return {"count": r.zlexcount(k, min, max)}


@zsets.post("/zpopmin")
def zset_zpopmin(k: str, count: int = 1, r: RDep = None):  # type: ignore[assignment]
    res = r.zpopmin(k, count)
    return {"items": [[m, float(s)] for m, s in res]}


@zsets.post("/zpopmax")
def zset_zpopmax(k: str, count: int = 1, r: RDep = None):  # type: ignore[assignment]
    res = r.zpopmax(k, count)
    return {"items": [[m, float(s)] for m, s in res]}


@zsets.post("/bzpopmin")
def zset_bzpopmin(keys: Annotated[list[str], Query()], timeout: float = 0.2, r: RDep = None):  # type: ignore[assignment]
    res = r.bzpopmin(keys, timeout=timeout)
    if res is None:
        return {"timeout": True}
    return {"key": res[0], "member": res[1], "score": float(res[2])}


@zsets.post("/bzpopmax")
def zset_bzpopmax(keys: Annotated[list[str], Query()], timeout: float = 0.2, r: RDep = None):  # type: ignore[assignment]
    res = r.bzpopmax(keys, timeout=timeout)
    if res is None:
        return {"timeout": True}
    return {"key": res[0], "member": res[1], "score": float(res[2])}


@zsets.post("/zrangestore")
def zset_zrangestore(dst: str, src: str, start: int = 0, stop: int = -1, r: RDep = None):  # type: ignore[assignment]
    return {"stored": r.zrangestore(dst, src, start, stop)}


@zsets.post("/zunionstore")
def zset_zunionstore(dst: str, keys: Annotated[list[str], Query()], r: RDep):
    return {"stored": r.zunionstore(dst, keys)}


@zsets.post("/zinterstore")
def zset_zinterstore(dst: str, keys: Annotated[list[str], Query()], r: RDep):
    return {"stored": r.zinterstore(dst, keys)}


@zsets.post("/zdiffstore")
def zset_zdiffstore(dst: str, keys: Annotated[list[str], Query()], r: RDep):
    return {"stored": r.zdiffstore(dst, keys)}


@zsets.get("/zscan")
def zset_zscan(k: str, match: str = "*", r: RDep = None):  # type: ignore[assignment]
    cur = 0
    pairs: list[tuple[str, float]] = []
    while True:
        cur, batch = r.zscan(k, cursor=cur, match=match)
        pairs.extend([(m, float(s)) for m, s in batch])
        if cur == 0:
            break
    pairs.sort(key=lambda x: (x[1], x[0]))
    return {"items": [[m, s] for m, s in pairs]}


@zsets.post("/zadd_inf")
def zset_zadd_inf(k: str, r: RDep):
    # Tests score infinities
    r.zadd(k, {"neg_inf": float("-inf"), "pos_inf": float("inf"), "zero": 0.0})
    inf = r.zscore(k, "pos_inf")
    ninf = r.zscore(k, "neg_inf")
    return {"pos_is_inf": inf == float("inf"), "neg_is_ninf": ninf == float("-inf")}


app.include_router(zsets)


# =====================================================================
# PUB/SUB
# =====================================================================
pubsub_r = APIRouter(prefix="/pubsub", tags=["pubsub"])


# Note: parity for pubsub is tricky because subscribers run in threads.
# We implement an explicit "subscribe+collect" endpoint that spawns a
# short-lived subscriber thread that collects messages for N seconds.

_SUB_STATE: dict[str, dict] = {}
_SUB_LOCK = threading.Lock()


def _subscriber_thread(sub_id: str, channels: list[str], patterns: list[str], duration: float):
    r = redis.Redis(connection_pool=_POOL)
    ps = r.pubsub(ignore_subscribe_messages=True)
    if channels:
        ps.subscribe(*channels)
    if patterns:
        ps.psubscribe(*patterns)
    received: list[dict] = []
    deadline = time.time() + duration
    while time.time() < deadline:
        msg = ps.get_message(timeout=0.05)
        if msg is None:
            continue
        received.append({
            "type": msg.get("type"),
            "channel": msg.get("channel"),
            "pattern": msg.get("pattern"),
            "data": msg.get("data"),
        })
    ps.close()
    with _SUB_LOCK:
        _SUB_STATE[sub_id]["messages"] = received
        _SUB_STATE[sub_id]["done"] = True


@pubsub_r.post("/subscribe_start")
def pubsub_subscribe_start(
    sub_id: str,
    channels: Annotated[list[str], Query(alias="c")] = [],
    patterns: Annotated[list[str], Query(alias="p")] = [],
    duration_ms: int = 500,
):
    with _SUB_LOCK:
        _SUB_STATE[sub_id] = {"messages": [], "done": False}
    t = threading.Thread(
        target=_subscriber_thread,
        args=(sub_id, channels, patterns, duration_ms / 1000.0),
        daemon=True,
    )
    t.start()
    # Give subscriber a moment to actually subscribe before caller publishes.
    time.sleep(0.1)
    return {"started": True}


@pubsub_r.post("/publish")
def pubsub_publish(channel: str, msg: str, r: RDep):
    return {"receivers": r.publish(channel, msg)}


@pubsub_r.get("/subscribe_result")
def pubsub_subscribe_result(sub_id: str, wait_ms: int = 600):
    deadline = time.time() + (wait_ms / 1000.0)
    while time.time() < deadline:
        with _SUB_LOCK:
            state = _SUB_STATE.get(sub_id)
            if state and state.get("done"):
                msgs = sorted(state["messages"], key=lambda m: (m["channel"] or "", str(m["data"])))
                return {"count": len(msgs), "messages": msgs}
        time.sleep(0.02)
    with _SUB_LOCK:
        state = _SUB_STATE.get(sub_id, {})
        msgs = sorted(state.get("messages", []), key=lambda m: (m["channel"] or "", str(m["data"])))
    return {"count": len(msgs), "messages": msgs, "incomplete": True}


app.include_router(pubsub_r)


# =====================================================================
# PIPELINES
# =====================================================================
pipes = APIRouter(prefix="/pipe", tags=["pipelines"])


@pipes.post("/simple")
def pipe_simple(key_prefix: str, r: RDep):
    pipe = r.pipeline()
    pipe.set(f"{key_prefix}:a", "1")
    pipe.set(f"{key_prefix}:b", "2")
    pipe.set(f"{key_prefix}:c", "3")
    pipe.incr(f"{key_prefix}:counter")
    pipe.incr(f"{key_prefix}:counter")
    res = pipe.execute()
    return {"results": [str(x) for x in res]}


@pipes.post("/transaction")
def pipe_transaction(key: str, r: RDep):
    # Classic MULTI/EXEC via pipeline(transaction=True)
    with r.pipeline(transaction=True) as pipe:
        pipe.set(key, "initial")
        pipe.incr(f"{key}:hits")
        pipe.incr(f"{key}:hits")
        pipe.get(key)
        res = pipe.execute()
    return {"results": [x.decode() if isinstance(x, bytes) else x for x in res]}


@pipes.post("/watch")
def pipe_watch(key: str, r: RDep):
    # Optimistic locking: set key, WATCH, compare, then MULTI/EXEC
    r.set(key, "100")
    with r.pipeline() as pipe:
        try:
            pipe.watch(key)
            current = int(pipe.get(key))
            pipe.multi()
            pipe.set(key, str(current + 1))
            pipe.execute()
            return {"ok": True, "final": r.get(key)}
        except redis.WatchError:
            return {"ok": False}


@pipes.post("/mixed")
def pipe_mixed(ns: str, r: RDep):
    pipe = r.pipeline()
    pipe.lpush(f"{ns}:list", "x", "y", "z")
    pipe.sadd(f"{ns}:set", "a", "b", "c")
    pipe.hset(f"{ns}:hash", mapping={"f1": "v1", "f2": "v2"})
    pipe.zadd(f"{ns}:zset", {"m1": 1.0, "m2": 2.0})
    res = pipe.execute()
    return {"ops": len(res), "values": [int(x) if isinstance(x, int) else str(x) for x in res]}


app.include_router(pipes)


# =====================================================================
# STREAMS
# =====================================================================
streams_r = APIRouter(prefix="/stream", tags=["streams"])


@streams_r.post("/xadd")
def stream_xadd(k: str, fields: Annotated[dict[str, str], Body()],
                maxlen: Optional[int] = None,
                nomkstream: bool = False, r: RDep = None):  # type: ignore[assignment]
    try:
        msg_id = r.xadd(k, fields, maxlen=maxlen, nomkstream=nomkstream)
        return {"id": msg_id}
    except redis.ResponseError as e:
        raise HTTPException(status_code=400, detail=str(e))


@streams_r.get("/xlen")
def stream_xlen(k: str, r: RDep):
    return {"length": r.xlen(k)}


@streams_r.get("/xrange")
def stream_xrange(k: str, r: RDep):
    items = r.xrange(k, min="-", max="+")
    return {"items": [[mid, fields] for mid, fields in items]}


@streams_r.get("/xrevrange")
def stream_xrevrange(k: str, r: RDep):
    items = r.xrevrange(k, max="+", min="-")
    return {"items": [[mid, fields] for mid, fields in items]}


@streams_r.get("/xread")
def stream_xread(k: str, start: str = "0", count: int = 100, r: RDep = None):  # type: ignore[assignment]
    res = r.xread({k: start}, count=count)
    norm = []
    for stream_name, entries in res or []:
        for mid, fields in entries:
            norm.append([stream_name, mid, fields])
    return {"entries": norm}


@streams_r.post("/xgroup_create")
def stream_xgroup_create(k: str, group: str, id: str = "$", mkstream: bool = True, r: RDep = None):  # type: ignore[assignment]
    try:
        r.xgroup_create(k, group, id=id, mkstream=mkstream)
        return {"created": True}
    except redis.ResponseError as e:
        return {"created": False, "error": str(e)}


@streams_r.post("/xreadgroup")
def stream_xreadgroup(k: str, group: str, consumer: str, count: int = 10, r: RDep = None):  # type: ignore[assignment]
    res = r.xreadgroup(group, consumer, {k: ">"}, count=count)
    norm = []
    for stream_name, entries in res or []:
        for mid, fields in entries:
            norm.append([stream_name, mid, fields])
    return {"entries": norm}


@streams_r.post("/xack")
def stream_xack(k: str, group: str, ids: Annotated[list[str], Query()], r: RDep):
    return {"acked": r.xack(k, group, *ids)}


@streams_r.get("/xpending")
def stream_xpending(k: str, group: str, r: RDep):
    p = r.xpending(k, group)
    return {"pending": p.get("pending") if isinstance(p, dict) else p[0]}


@streams_r.post("/xtrim")
def stream_xtrim(k: str, maxlen: int, r: RDep):
    return {"trimmed": r.xtrim(k, maxlen=maxlen)}


@streams_r.post("/xdel")
def stream_xdel(k: str, ids: Annotated[list[str], Query()], r: RDep):
    return {"deleted": r.xdel(k, *ids)}


app.include_router(streams_r)


# =====================================================================
# SCRIPTING (Lua)
# =====================================================================
lua = APIRouter(prefix="/lua", tags=["scripting"])


@lua.post("/eval")
def lua_eval(body: Annotated[dict, Body()], r: RDep):
    """Body: {script, keys: [], args: []}."""
    script = body.get("script", "")
    keys = body.get("keys", [])
    args = body.get("args", [])
    res = r.eval(script, len(keys), *keys, *args)
    # Normalize bytes if any
    if isinstance(res, bytes):
        res = res.decode()
    return {"result": res}


@lua.post("/load_and_evalsha")
def lua_load_and_evalsha(body: Annotated[dict, Body()], r: RDep):
    script = body.get("script", "")
    keys = body.get("keys", [])
    args = body.get("args", [])
    sha = r.script_load(script)
    res = r.evalsha(sha, len(keys), *keys, *args)
    if isinstance(res, bytes):
        res = res.decode()
    return {"sha_len": len(sha), "result": res}


@lua.post("/incr_if_eq")
def lua_incr_if_eq(k: str, expected: str, r: RDep):
    script = """
    local v = redis.call('GET', KEYS[1])
    if v == ARGV[1] then
      return redis.call('INCR', KEYS[1])
    else
      return -1
    end
    """
    return {"result": r.eval(script, 1, k, expected)}


app.include_router(lua)


# =====================================================================
# HIGH-LEVEL APP PATTERNS
# =====================================================================
patterns = APIRouter(prefix="/app", tags=["patterns"])


# ── Cache decorator ──
def cache_get_or_set(r: redis.Redis, key: str, ttl: int, producer):
    hit = r.get(key)
    if hit is not None:
        return {"cached": True, "value": hit}
    v = producer()
    r.setex(key, ttl, v)
    return {"cached": False, "value": v}


@patterns.get("/cache/hello")
def cache_hello(name: str, r: RDep):
    key = f"cache:hello:{name}"
    return cache_get_or_set(r, key, 60, lambda: f"hello, {name}")


# ── Rate limiter ──
@patterns.post("/ratelimit")
def ratelimit(user: str, limit: int = 3, window: int = 60, r: RDep = None):  # type: ignore[assignment]
    key = f"rl:{user}"
    n = r.incr(key)
    if n == 1:
        r.expire(key, window)
    if n > limit:
        raise HTTPException(status_code=429, detail="rate limited")
    return {"count": n, "limit": limit}


# ── Session store ──
@patterns.post("/session/create")
def session_create(user: str, r: RDep):
    sid = f"sess:{user}:{int(time.time()*1000) % 100000}"
    r.setex(sid, 300, json.dumps({"user": user, "ctime": 0}))
    return {"session_id": sid}


@patterns.get("/session/get")
def session_get(sid: str, r: RDep):
    v = r.get(sid)
    if v is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"session": json.loads(v)}


# ── Feature flags ──
@patterns.post("/flags/set")
def flags_set(mapping: Annotated[dict[str, str], Body()], r: RDep):
    r.hset("flags", mapping=mapping)
    return {"ok": True, "count": len(mapping)}


@patterns.get("/flags/all")
def flags_all(r: RDep):
    return {"flags": r.hgetall("flags")}


@patterns.get("/flags/one")
def flags_one(name: str, r: RDep):
    v = r.hget("flags", name)
    return {"value": v, "enabled": v == "on"}


# ── Leaderboard ──
@patterns.post("/lb/add")
def lb_add(user: str, score: float, r: RDep):
    r.zadd("leaderboard", {user: score})
    return {"ok": True, "rank": r.zrevrank("leaderboard", user)}


@patterns.get("/lb/top")
def lb_top(n: int = 5, r: RDep = None):  # type: ignore[assignment]
    items = r.zrevrange("leaderboard", 0, n - 1, withscores=True)
    return {"top": [[m, float(s)] for m, s in items]}


# ── Distributed lock ──
@patterns.post("/lock/acquire")
def lock_acquire(name: str, ttl_ms: int = 2000, r: RDep = None):  # type: ignore[assignment]
    token = f"tok-{int(time.time()*1000) % 1000000}"
    ok = r.set(f"lock:{name}", token, nx=True, px=ttl_ms)
    return {"acquired": bool(ok), "token": token if ok else None}


@patterns.post("/lock/release")
def lock_release(name: str, token: str, r: RDep):
    script = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('DEL', KEYS[1])
    else
      return 0
    end
    """
    return {"released": bool(r.eval(script, 1, f"lock:{name}", token))}


app.include_router(patterns)


# =====================================================================
# SERIALIZATION
# =====================================================================
serial = APIRouter(prefix="/ser", tags=["serialization"])


@serial.post("/utf8")
def ser_utf8(k: str, v: str, r: RDep):
    r.set(k, v)
    return {"value": r.get(k)}


@serial.post("/unicode")
def ser_unicode(k: str, r: RDep):
    s = "héllo 🌍 こんにちは ∞"
    r.set(k, s)
    return {"value": r.get(k), "len_chars": len(s)}


@serial.post("/json")
def ser_json(k: str, payload: Annotated[dict, Body()], r: RDep):
    r.set(k, json.dumps(payload, sort_keys=True))
    return {"value": json.loads(r.get(k))}


@serial.post("/bytes")
def ser_bytes(k: str, v: str, r: RDep):
    raw = get_raw()
    raw.set(k, v.encode())
    got = raw.get(k)
    return {"length": len(got or b""), "equal": got == v.encode()}


app.include_router(serial)


# =====================================================================
# ERROR HANDLING
# =====================================================================
errs = APIRouter(prefix="/err", tags=["errors"])


@errs.get("/wrongtype")
def err_wrongtype(k: str, r: RDep):
    # Pre-seed as string, then read as list.
    r.set(k, "not-a-list")
    try:
        r.lpush(k, "x")
        return {"error": None}
    except redis.ResponseError as e:
        raise HTTPException(status_code=409, detail=str(e).split()[0])  # "WRONGTYPE"


@errs.get("/nonexistent_get")
def err_nonexistent_get(r: RDep):
    # r.get on missing key yields None (not error)
    return {"value": r.get("definitely-missing-key-xyz")}


@errs.get("/type_nonexistent")
def err_type_nonexistent(r: RDep):
    return {"type": r.type("definitely-missing-key-xyz")}


@errs.get("/ttl_nonexistent")
def err_ttl_nonexistent(r: RDep):
    return {"ttl": r.ttl("definitely-missing-key-xyz")}


app.include_router(errs)


# =====================================================================
# ADMIN: flush / introspect (used by runner between tests)
# =====================================================================
admin = APIRouter(prefix="/_admin", tags=["admin"])


@admin.post("/flushdb")
def admin_flushdb(r: RDep):
    r.flushdb()
    return {"ok": True}


@admin.get("/dbsize")
def admin_dbsize(r: RDep):
    return {"size": r.dbsize()}


@admin.get("/info_keyspace")
def admin_info_keyspace(r: RDep):
    info = r.info("keyspace")
    return {"keys_db0": info.get("db0", {}).get("keys", 0) if isinstance(info.get("db0"), dict) else 0}


app.include_router(admin)
