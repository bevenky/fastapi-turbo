#!/usr/bin/env python3
"""Redis integration parity runner.

For each of parity_app_redis (sync) and parity_app_redis_async (async):
  - boots stock FastAPI via uvicorn
  - boots fastapi-turbo
  - runs ~250 tests that exercise Redis commands + high-level patterns
  - compares (status, body-json, selected headers) between FA and FR
  - clears Redis (FLUSHDB) before every test so state is isolated
  - after each test, verifies resulting Redis state via an external client

Ports: 29960 (FA sync), 29961 (FR sync), 29970 (FA async), 29971 (FR async)
Redis: 127.0.0.1:6392
"""
from __future__ import annotations

import copy
import json
import os
import socket
import subprocess
import sys
import time
import traceback
from collections import defaultdict
from typing import Any, Callable

import httpx
import redis as redis_sync


# ── Config ─────────────────────────────────────────────────────────

HOST = "127.0.0.1"
FA_SYNC_PORT = 29960
FR_SYNC_PORT = 29961
FA_ASYNC_PORT = 29970
FR_ASYNC_PORT = 29971
REDIS_PORT = 6392
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STARTUP_TIMEOUT = 25

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
RESET = "\033[0m"
BOLD = "\033[1m"


# ── Redis ensure/verify ────────────────────────────────────────────

def redis_ping() -> bool:
    try:
        c = redis_sync.Redis(host=HOST, port=REDIS_PORT, socket_connect_timeout=1)
        c.ping()
        c.close()
        return True
    except Exception:
        return False


def ensure_redis():
    """Ping, else try to start a docker container on 6392."""
    if redis_ping():
        print(f"{GREEN}Redis already available on :{REDIS_PORT}{RESET}")
        return
    # Try to start docker container
    print(f"{YELLOW}Redis not responding on :{REDIS_PORT}; trying docker…{RESET}")
    # Remove any stale container
    subprocess.run(["docker", "rm", "-f", "fastapi_turbo_redis"], capture_output=True)
    p = subprocess.run(
        ["docker", "run", "-d", "--name", "fastapi_turbo_redis",
         "-p", f"{REDIS_PORT}:6379", "redis:7"],
        capture_output=True,
    )
    if p.returncode != 0:
        print(p.stderr.decode(errors="ignore"))
        raise SystemExit("Failed to launch Redis via docker.")
    # Wait for ping.
    deadline = time.time() + 30
    while time.time() < deadline:
        if redis_ping():
            print(f"{GREEN}Redis up via docker on :{REDIS_PORT}{RESET}")
            return
        time.sleep(0.3)
    raise SystemExit("Redis failed to come up.")


VERIFY = redis_sync.Redis(host=HOST, port=REDIS_PORT, decode_responses=True)
VERIFY_RAW = redis_sync.Redis(host=HOST, port=REDIS_PORT, decode_responses=False)


def flushdb():
    VERIFY.flushdb()


# ── Server management ──────────────────────────────────────────────

def wait_for_port(port, timeout=STARTUP_TIMEOUT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, port), timeout=1):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.2)
    return False


def start_uvicorn(port: int, app_module: str):
    env = os.environ.copy()
    env["PYTHONPATH"] = PROJECT_ROOT
    env.pop("FASTAPI_TURBO", None)
    env["REDIS_HOST"] = HOST
    env["REDIS_PORT"] = str(REDIS_PORT)
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", app_module,
         "--host", HOST, "--port", str(port), "--log-level", "warning"],
        cwd=PROJECT_ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def start_fastapi_turbo(port: int, app_import: str):
    env = os.environ.copy()
    env["PYTHONPATH"] = PROJECT_ROOT
    env["REDIS_HOST"] = HOST
    env["REDIS_PORT"] = str(REDIS_PORT)
    script = f"""
import sys
sys.path.insert(0, '{PROJECT_ROOT}')
from fastapi_turbo.compat import install
install()
from {app_import} import app
app.run(host='{HOST}', port={port})
"""
    return subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


# ── Results ────────────────────────────────────────────────────────

class Suite:
    def __init__(self, label: str):
        self.label = label
        self.results: list[tuple[int, str, str, bool, str]] = []

    def record(self, tid: int, category: str, desc: str, passed: bool, detail: str = ""):
        self.results.append((tid, category, desc, passed, detail))
        if not passed:
            print(f"  {RED}FAIL{RESET} [{self.label}] T{tid:04d} [{category}] {desc}")
            if detail:
                print(f"        {detail[:220]}")


# ── HTTP helpers ───────────────────────────────────────────────────

_CLIENTS: dict[str, httpx.Client] = {}


def _client(base: str) -> httpx.Client:
    c = _CLIENTS.get(base)
    if c is None:
        # Short timeout: when fastapi-turbo hangs on a broken handler we don't
        # want to wait 10s per request. 1.2s is plenty for Redis ops.
        c = httpx.Client(base_url=base, timeout=1.2, follow_redirects=False)
        _CLIENTS[base] = c
    return c


# Circuit breaker: when FR consistently fails for a long stretch we can
# mark the rest of the suite as "FR-broken" and short-circuit future requests
# to the FR base URL. We still record them as failures but don't wait.
_CIRCUIT = {"fr_broken_suite": None, "fr_fail_streak": 0}


def _fr_broken(fr_base: str) -> bool:
    return _CIRCUIT["fr_broken_suite"] == fr_base


def _tick_fr_result(fr: Any, fr_base: str):
    if isinstance(fr, Exception) or (hasattr(fr, "status_code") and fr.status_code >= 500):
        _CIRCUIT["fr_fail_streak"] += 1
        if _CIRCUIT["fr_fail_streak"] >= 20 and not _fr_broken(fr_base):
            _CIRCUIT["fr_broken_suite"] = fr_base
            print(f"{RED}⚠ fastapi-turbo broken: tripping circuit breaker on {fr_base}{RESET}")
    else:
        _CIRCUIT["fr_fail_streak"] = 0


def _reset_circuit():
    _CIRCUIT["fr_broken_suite"] = None
    _CIRCUIT["fr_fail_streak"] = 0


def close_clients():
    for c in _CLIENTS.values():
        try:
            c.close()
        except Exception:
            pass
    _CLIENTS.clear()


def both_req(fa_base: str, fr_base: str, method: str, path: str,
             seed: Callable[[], None] | None = None,
             shared: bool = False, **kw):
    """Issue the same request to both servers.

    Default: ISOLATE — flushdb before each side, apply `seed()` if given,
    then call. This way commands that mutate state (INCR, SETNX, POP, etc.)
    produce identical results on both servers.

    shared=True: skip isolation. Both servers share whatever state exists.
    Use this for multi-step flows where earlier calls set up Redis.
    """
    fa: Any = None
    fr: Any = None
    if not shared:
        flushdb()
        if seed is not None:
            seed()
    try:
        fa = _client(fa_base).request(method, path, **kw)
    except Exception as e:
        fa = e
    # Circuit-break FR calls if the suite is known to be broken.
    if _fr_broken(fr_base):
        fr = TimeoutError("fr-broken (circuit-breaker)")
    else:
        if not shared:
            flushdb()
            if seed is not None:
                seed()
        try:
            fr = _client(fr_base).request(method, path, **kw)
        except Exception as e:
            fr = e
        _tick_fr_result(fr, fr_base)
    return fa, fr


def r_status(r: Any) -> int:
    if isinstance(r, Exception):
        return -1
    return r.status_code


def r_json(r: Any) -> Any:
    if isinstance(r, Exception):
        return None
    try:
        return r.json()
    except Exception:
        return None


def r_text(r: Any) -> str:
    if isinstance(r, Exception):
        return f"EXC: {r}"
    try:
        return r.text
    except Exception:
        return ""


def _normalize(obj):
    """Normalize JSON for comparison: sort list-of-scalars where semantics
    permit. Redis LRANGE returns ordered lists so we usually don't want
    this. Callers pick when to sort. Here we just deep-copy."""
    return copy.deepcopy(obj)


def expect_equal(fa, fr, expected_status: int | None = None, compare_body: bool = True,
                 sort_list_keys: list[str] | None = None):
    fa_s, fr_s = r_status(fa), r_status(fr)
    if expected_status is not None:
        if fa_s != expected_status:
            raise AssertionError(f"FA status={fa_s} expected {expected_status}; body={r_text(fa)[:200]}")
        if fr_s != expected_status:
            raise AssertionError(f"FR status={fr_s} expected {expected_status}; body={r_text(fr)[:200]}")
    if fa_s != fr_s:
        raise AssertionError(f"status mismatch: FA={fa_s} FR={fr_s}; FA={r_text(fa)[:160]}; FR={r_text(fr)[:160]}")
    if compare_body:
        fa_j = r_json(fa); fr_j = r_json(fr)
        if sort_list_keys and isinstance(fa_j, dict) and isinstance(fr_j, dict):
            for key in sort_list_keys:
                if isinstance(fa_j.get(key), list):
                    fa_j = {**fa_j, key: sorted(fa_j[key], key=lambda x: json.dumps(x, sort_keys=True))}
                if isinstance(fr_j.get(key), list):
                    fr_j = {**fr_j, key: sorted(fr_j[key], key=lambda x: json.dumps(x, sort_keys=True))}
        if fa_j != fr_j:
            raise AssertionError(
                f"body mismatch:\n  FA={json.dumps(fa_j, sort_keys=True)[:300]}\n  FR={json.dumps(fr_j, sort_keys=True)[:300]}"
            )


def expect_redis_state(check: Callable[[redis_sync.Redis], None]):
    check(VERIFY)


# ── Test definitions ──────────────────────────────────────────────

def run_test(suite: Suite, tid: int, cat: str, desc: str,
             do: Callable[[], None], *, flush: bool = True) -> int:
    if flush:
        try:
            flushdb()
        except Exception:
            pass
    try:
        do()
        suite.record(tid, cat, desc, True)
    except AssertionError as e:
        suite.record(tid, cat, desc, False, str(e))
    except Exception as e:
        suite.record(tid, cat, desc, False, f"{type(e).__name__}: {e}")
    return tid + 1


# =====================================================================
# SHARED test library (runs against both sync-app and async-app bases)
# =====================================================================

def run_suite(suite: Suite, fa_base: str, fr_base: str):
    tid = 1

    # ── STRINGS ────────────────────────────────────────────────
    cat = "strings"

    def t_set_get():
        fa, fr = both_req(fa_base, fr_base, "POST", "/str/set?k=x", json={"value": "hello"})
        expect_equal(fa, fr, 200)
        fa2, fr2 = both_req(fa_base, fr_base, "GET", "/str/get?k=x",
                            seed=lambda: VERIFY.set("x", "hello"))
        expect_equal(fa2, fr2, 200)
    tid = run_test(suite, tid, cat, "SET + GET", t_set_get)

    def t_set_missing_value_422():
        fa, fr = both_req(fa_base, fr_base, "POST", "/str/set?k=x", json={})
        expect_equal(fa, fr, 422, compare_body=False)
    tid = run_test(suite, tid, cat, "SET missing body → 422", t_set_missing_value_422)

    def t_set_ex():
        fa, fr = both_req(fa_base, fr_base, "POST", "/str/set?k=x", json={"value": "v", "ex": 60})
        expect_equal(fa, fr, 200)
        assert VERIFY.ttl("x") > 0
    tid = run_test(suite, tid, cat, "SET EX=60 → TTL>0", t_set_ex)

    def t_set_nx_new():
        fa, fr = both_req(fa_base, fr_base, "POST", "/str/set?k=y", json={"value": "v1", "nx": True})
        expect_equal(fa, fr, 200)
        assert VERIFY.get("y") == "v1"
    tid = run_test(suite, tid, cat, "SETNX new → ok=True", t_set_nx_new)

    def t_set_nx_exists():
        fa, fr = both_req(fa_base, fr_base, "POST", "/str/set?k=y", json={"value": "v1", "nx": True},
                          seed=lambda: VERIFY.set("y", "pre"))
        expect_equal(fa, fr, 200)
        assert VERIFY.get("y") == "pre"
    tid = run_test(suite, tid, cat, "SETNX exists → ok=False, no change", t_set_nx_exists)

    def t_getset():
        fa, fr = both_req(fa_base, fr_base, "POST", "/str/getset?k=z&v=new",
                          seed=lambda: VERIFY.set("z", "old"))
        expect_equal(fa, fr, 200)
        assert VERIFY.get("z") == "new"
    tid = run_test(suite, tid, cat, "GETSET", t_getset)

    def t_setex():
        fa, fr = both_req(fa_base, fr_base, "POST", "/str/setex?k=t&seconds=42&v=hi")
        expect_equal(fa, fr, 200, compare_body=False)  # ttl readback may differ
        assert 40 <= VERIFY.ttl("t") <= 42
    tid = run_test(suite, tid, cat, "SETEX", t_setex)

    def t_psetex():
        fa, fr = both_req(fa_base, fr_base, "POST", "/str/psetex?k=p&ms=5000&v=hi")
        expect_equal(fa, fr, 200)
        assert VERIFY.pttl("p") > 0
    tid = run_test(suite, tid, cat, "PSETEX", t_psetex)

    def t_setnx_raw():
        fa, fr = both_req(fa_base, fr_base, "POST", "/str/setnx?k=n&v=fresh")
        expect_equal(fa, fr, 200)
        assert VERIFY.get("n") == "fresh"
    tid = run_test(suite, tid, cat, "SETNX command created=True", t_setnx_raw)

    def t_setnx_raw_exists():
        fa, fr = both_req(fa_base, fr_base, "POST", "/str/setnx?k=n&v=fresh",
                          seed=lambda: VERIFY.set("n", "pre"))
        expect_equal(fa, fr, 200)
        assert VERIFY.get("n") == "pre"
    tid = run_test(suite, tid, cat, "SETNX command exists=False", t_setnx_raw_exists)

    def t_mset():
        fa, fr = both_req(fa_base, fr_base, "POST", "/str/mset", json={"a": "1", "b": "2", "c": "3"})
        expect_equal(fa, fr, 200)
        assert VERIFY.mget(["a", "b", "c"]) == ["1", "2", "3"]
    tid = run_test(suite, tid, cat, "MSET", t_mset)

    def t_mget():
        fa, fr = both_req(fa_base, fr_base, "GET", "/str/mget?keys=a&keys=b&keys=missing",
                          seed=lambda: VERIFY.mset({"a": "1", "b": "2"}))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "MGET with missing key", t_mget)

    def t_getrange():
        fa, fr = both_req(fa_base, fr_base, "GET", "/str/getrange?k=s&start=0&end=4",
                          seed=lambda: VERIFY.set("s", "hello world"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "GETRANGE 0-4", t_getrange)

    def t_setrange():
        fa, fr = both_req(fa_base, fr_base, "POST", "/str/setrange?k=s&offset=6&v=REDIS",
                          seed=lambda: VERIFY.set("s", "hello world"))
        expect_equal(fa, fr, 200)
        assert VERIFY.get("s") == "hello REDIS"
    tid = run_test(suite, tid, cat, "SETRANGE", t_setrange)

    def t_strlen():
        fa, fr = both_req(fa_base, fr_base, "GET", "/str/strlen?k=s",
                          seed=lambda: VERIFY.set("s", "abc"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "STRLEN", t_strlen)

    def t_incr():
        fa, fr = both_req(fa_base, fr_base, "POST", "/str/incr?k=c")
        expect_equal(fa, fr, 200)
        assert VERIFY.get("c") == "1"
    tid = run_test(suite, tid, cat, "INCR first → 1", t_incr)

    def t_decr():
        fa, fr = both_req(fa_base, fr_base, "POST", "/str/decr?k=c",
                          seed=lambda: VERIFY.set("c", "5"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "DECR", t_decr)

    def t_incrby():
        fa, fr = both_req(fa_base, fr_base, "POST", "/str/incrby?k=c&n=10")
        expect_equal(fa, fr, 200)
        assert VERIFY.get("c") == "10"
    tid = run_test(suite, tid, cat, "INCRBY 10", t_incrby)

    def t_decrby():
        fa, fr = both_req(fa_base, fr_base, "POST", "/str/decrby?k=c&n=7",
                          seed=lambda: VERIFY.set("c", "100"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "DECRBY 7", t_decrby)

    def t_incrbyfloat():
        fa, fr = both_req(fa_base, fr_base, "POST", "/str/incrbyfloat?k=f&n=1.5")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "INCRBYFLOAT 1.5", t_incrbyfloat)

    def t_append():
        fa, fr = both_req(fa_base, fr_base, "POST", "/str/append?k=s&v=XYZ",
                          seed=lambda: VERIFY.set("s", "abc"))
        expect_equal(fa, fr, 200)
        assert VERIFY.get("s") == "abcXYZ"
    tid = run_test(suite, tid, cat, "APPEND", t_append)

    def t_expire():
        fa, fr = both_req(fa_base, fr_base, "POST", "/str/expire?k=k&seconds=30",
                          seed=lambda: VERIFY.set("k", "v"))
        expect_equal(fa, fr, 200)
        assert 0 < VERIFY.ttl("k") <= 30
    tid = run_test(suite, tid, cat, "EXPIRE 30", t_expire)

    def t_expireat():
        future = int(time.time()) + 60
        fa, fr = both_req(fa_base, fr_base, "POST", f"/str/expireat?k=k&ts={future}",
                          seed=lambda: VERIFY.set("k", "v"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "EXPIREAT", t_expireat)

    def t_persist():
        fa, fr = both_req(fa_base, fr_base, "POST", "/str/persist?k=k",
                          seed=lambda: VERIFY.setex("k", 100, "v"))
        expect_equal(fa, fr, 200)
        assert VERIFY.ttl("k") == -1
    tid = run_test(suite, tid, cat, "PERSIST", t_persist)

    def t_ttl():
        fa, fr = both_req(fa_base, fr_base, "GET", "/str/ttl?k=k",
                          seed=lambda: VERIFY.setex("k", 99, "v"))
        expect_equal(fa, fr, 200, compare_body=False)
        j1, j2 = r_json(fa), r_json(fr)
        assert isinstance(j1, dict) and isinstance(j2, dict)
        t1, t2 = j1["ttl"], j2["ttl"]
        assert 95 <= t1 <= 99 and 95 <= t2 <= 99
    tid = run_test(suite, tid, cat, "TTL", t_ttl)

    def t_pttl():
        fa, fr = both_req(fa_base, fr_base, "GET", "/str/pttl?k=k",
                          seed=lambda: VERIFY.psetex("k", 5000, "v"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "PTTL (bucketed)", t_pttl)

    def t_type_string():
        fa, fr = both_req(fa_base, fr_base, "GET", "/str/type?k=k",
                          seed=lambda: VERIFY.set("k", "v"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "TYPE string", t_type_string)

    def t_type_none():
        fa, fr = both_req(fa_base, fr_base, "GET", "/str/type?k=nope")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "TYPE nonexistent → 'none'", t_type_none)

    def t_exists():
        fa, fr = both_req(fa_base, fr_base, "GET", "/str/exists?keys=a&keys=b&keys=c",
                          seed=lambda: VERIFY.mset({"a": "1", "b": "2"}))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "EXISTS multi", t_exists)

    def t_del():
        fa, fr = both_req(fa_base, fr_base, "POST", "/str/del?keys=a&keys=b&keys=nope",
                          seed=lambda: VERIFY.mset({"a": "1", "b": "2"}))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "DEL multi", t_del)

    def t_unlink():
        fa, fr = both_req(fa_base, fr_base, "POST", "/str/unlink?keys=a&keys=b",
                          seed=lambda: VERIFY.mset({"a": "1", "b": "2"}))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "UNLINK", t_unlink)

    def t_keys():
        fa, fr = both_req(fa_base, fr_base, "GET", "/str/keys?pattern=u:*",
                          seed=lambda: VERIFY.mset({"u:1": "a", "u:2": "b", "u:3": "c", "x": "y"}))
        expect_equal(fa, fr, 200, sort_list_keys=["keys"])
    tid = run_test(suite, tid, cat, "KEYS pattern", t_keys)

    def t_scan():
        fa, fr = both_req(fa_base, fr_base, "GET", "/str/scan?match=scan:*&count=5",
                          seed=lambda: VERIFY.mset({f"scan:{i}": str(i) for i in range(20)}))
        expect_equal(fa, fr, 200, sort_list_keys=["keys"])
    tid = run_test(suite, tid, cat, "SCAN cursor iteration", t_scan)

    # ── LISTS ──────────────────────────────────────────────────
    cat = "lists"

    def t_lpush():
        fa, fr = both_req(fa_base, fr_base, "POST", "/list/lpush?k=L&v=a&v=b&v=c")
        expect_equal(fa, fr, 200)
        assert VERIFY.lrange("L", 0, -1) == ["c", "b", "a"]
    tid = run_test(suite, tid, cat, "LPUSH multi", t_lpush)

    def t_rpush():
        fa, fr = both_req(fa_base, fr_base, "POST", "/list/rpush?k=L&v=a&v=b&v=c")
        expect_equal(fa, fr, 200)
        assert VERIFY.lrange("L", 0, -1) == ["a", "b", "c"]
    tid = run_test(suite, tid, cat, "RPUSH multi", t_rpush)

    def t_lpop():
        fa, fr = both_req(fa_base, fr_base, "POST", "/list/lpop?k=L",
                          seed=lambda: VERIFY.rpush("L", "a", "b", "c"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "LPOP", t_lpop)

    def t_lpop_count():
        fa, fr = both_req(fa_base, fr_base, "POST", "/list/lpop?k=L&count=2",
                          seed=lambda: VERIFY.rpush("L", "a", "b", "c", "d"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "LPOP count=2", t_lpop_count)

    def t_rpop():
        fa, fr = both_req(fa_base, fr_base, "POST", "/list/rpop?k=L",
                          seed=lambda: VERIFY.rpush("L", "a", "b", "c"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "RPOP", t_rpop)

    def t_rpop_count():
        fa, fr = both_req(fa_base, fr_base, "POST", "/list/rpop?k=L&count=2",
                          seed=lambda: VERIFY.rpush("L", "a", "b", "c", "d"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "RPOP count=2", t_rpop_count)

    def t_lrange():
        fa, fr = both_req(fa_base, fr_base, "GET", "/list/lrange?k=L&start=0&stop=4",
                          seed=lambda: VERIFY.rpush("L", *[str(i) for i in range(10)]))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "LRANGE 0-4", t_lrange)

    def t_lrange_negative():
        fa, fr = both_req(fa_base, fr_base, "GET", "/list/lrange?k=L&start=-3&stop=-1",
                          seed=lambda: VERIFY.rpush("L", *[str(i) for i in range(10)]))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "LRANGE -3..-1", t_lrange_negative)

    def t_llen():
        fa, fr = both_req(fa_base, fr_base, "GET", "/list/llen?k=L",
                          seed=lambda: VERIFY.rpush("L", "a", "b", "c"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "LLEN", t_llen)

    def t_lindex():
        fa, fr = both_req(fa_base, fr_base, "GET", "/list/lindex?k=L&i=1",
                          seed=lambda: VERIFY.rpush("L", "a", "b", "c"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "LINDEX", t_lindex)

    def t_lset_ok():
        fa, fr = both_req(fa_base, fr_base, "POST", "/list/lset?k=L&i=1&v=B",
                          seed=lambda: VERIFY.rpush("L", "a", "b", "c"))
        expect_equal(fa, fr, 200)
        assert VERIFY.lrange("L", 0, -1) == ["a", "B", "c"]
    tid = run_test(suite, tid, cat, "LSET in-range", t_lset_ok)

    def t_lset_bad():
        fa, fr = both_req(fa_base, fr_base, "POST", "/list/lset?k=L&i=10&v=X")
        expect_equal(fa, fr, 400, compare_body=False)
    tid = run_test(suite, tid, cat, "LSET out-of-range → 400", t_lset_bad)

    def t_linsert():
        fa, fr = both_req(fa_base, fr_base, "POST", "/list/linsert?k=L&where=BEFORE&pivot=c&v=b",
                          seed=lambda: VERIFY.rpush("L", "a", "c"))
        expect_equal(fa, fr, 200)
        assert VERIFY.lrange("L", 0, -1) == ["a", "b", "c"]
    tid = run_test(suite, tid, cat, "LINSERT BEFORE", t_linsert)

    def t_lrem():
        fa, fr = both_req(fa_base, fr_base, "POST", "/list/lrem?k=L&count=2&v=a",
                          seed=lambda: VERIFY.rpush("L", "a", "b", "a", "c", "a"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "LREM count=2", t_lrem)

    def t_ltrim():
        fa, fr = both_req(fa_base, fr_base, "POST", "/list/ltrim?k=L&start=1&stop=4",
                          seed=lambda: VERIFY.rpush("L", *[str(i) for i in range(10)]))
        expect_equal(fa, fr, 200)
        assert VERIFY.lrange("L", 0, -1) == ["1", "2", "3", "4"]
    tid = run_test(suite, tid, cat, "LTRIM", t_ltrim)

    def t_rpoplpush():
        fa, fr = both_req(fa_base, fr_base, "POST", "/list/rpoplpush?src=src&dst=dst",
                          seed=lambda: VERIFY.rpush("src", "a", "b", "c"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "RPOPLPUSH", t_rpoplpush)

    def t_lmove():
        fa, fr = both_req(fa_base, fr_base, "POST", "/list/lmove?src=src&dst=dst&where_from=LEFT&where_to=RIGHT",
                          seed=lambda: VERIFY.rpush("src", "a", "b", "c"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "LMOVE", t_lmove)

    def t_blpop_timeout():
        fa, fr = both_req(fa_base, fr_base, "POST", "/list/blpop?keys=nope&timeout=0.15")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "BLPOP timeout", t_blpop_timeout)

    def t_blpop_has():
        fa, fr = both_req(fa_base, fr_base, "POST", "/list/blpop?keys=L&timeout=0.5",
                          seed=lambda: VERIFY.rpush("L", "a", "b"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "BLPOP with data", t_blpop_has)

    def t_brpop_timeout():
        fa, fr = both_req(fa_base, fr_base, "POST", "/list/brpop?keys=nope&timeout=0.15")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "BRPOP timeout", t_brpop_timeout)

    def t_lpushx_nope():
        fa, fr = both_req(fa_base, fr_base, "POST", "/list/lpushx?k=nope&v=x")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "LPUSHX nonexistent", t_lpushx_nope)

    def t_lpushx_exists():
        fa, fr = both_req(fa_base, fr_base, "POST", "/list/lpushx?k=L&v=x",
                          seed=lambda: VERIFY.rpush("L", "a"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "LPUSHX existing", t_lpushx_exists)

    def t_rpushx_nope():
        fa, fr = both_req(fa_base, fr_base, "POST", "/list/rpushx?k=nope&v=x")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "RPUSHX nonexistent", t_rpushx_nope)

    # ── HASHES ─────────────────────────────────────────────────
    cat = "hashes"

    def t_hset():
        fa, fr = both_req(fa_base, fr_base, "POST", "/hash/hset?k=H", json={"f1": "v1", "f2": "v2"})
        expect_equal(fa, fr, 200)
        assert VERIFY.hgetall("H") == {"f1": "v1", "f2": "v2"}
    tid = run_test(suite, tid, cat, "HSET mapping", t_hset)

    def t_hget():
        fa, fr = both_req(fa_base, fr_base, "GET", "/hash/hget?k=H&f=f",
                          seed=lambda: VERIFY.hset("H", "f", "v"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "HGET", t_hget)

    def t_hget_missing():
        fa, fr = both_req(fa_base, fr_base, "GET", "/hash/hget?k=H&f=missing",
                          seed=lambda: VERIFY.hset("H", "f", "v"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "HGET missing field", t_hget_missing)

    def t_hmset():
        fa, fr = both_req(fa_base, fr_base, "POST", "/hash/hmset?k=H", json={"a": "1", "b": "2"})
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "HMSET (deprecated but supported)", t_hmset)

    def t_hmget():
        fa, fr = both_req(fa_base, fr_base, "GET", "/hash/hmget?k=H&f=a&f=b&f=c",
                          seed=lambda: VERIFY.hset("H", mapping={"a": "1", "b": "2"}))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "HMGET", t_hmget)

    def t_hdel():
        fa, fr = both_req(fa_base, fr_base, "POST", "/hash/hdel?k=H&f=a&f=b",
                          seed=lambda: VERIFY.hset("H", mapping={"a": "1", "b": "2", "c": "3"}))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "HDEL multi", t_hdel)

    def t_hexists():
        fa, fr = both_req(fa_base, fr_base, "GET", "/hash/hexists?k=H&f=f",
                          seed=lambda: VERIFY.hset("H", "f", "v"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "HEXISTS yes", t_hexists)

    def t_hexists_no():
        fa, fr = both_req(fa_base, fr_base, "GET", "/hash/hexists?k=H&f=nope")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "HEXISTS no", t_hexists_no)

    def t_hkeys():
        fa, fr = both_req(fa_base, fr_base, "GET", "/hash/hkeys?k=H",
                          seed=lambda: VERIFY.hset("H", mapping={"a": "1", "b": "2"}))
        expect_equal(fa, fr, 200, sort_list_keys=["keys"])
    tid = run_test(suite, tid, cat, "HKEYS", t_hkeys)

    def t_hvals():
        fa, fr = both_req(fa_base, fr_base, "GET", "/hash/hvals?k=H",
                          seed=lambda: VERIFY.hset("H", mapping={"a": "1", "b": "2"}))
        expect_equal(fa, fr, 200, sort_list_keys=["values"])
    tid = run_test(suite, tid, cat, "HVALS", t_hvals)

    def t_hgetall():
        fa, fr = both_req(fa_base, fr_base, "GET", "/hash/hgetall?k=H",
                          seed=lambda: VERIFY.hset("H", mapping={"a": "1", "b": "2"}))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "HGETALL", t_hgetall)

    def t_hlen():
        fa, fr = both_req(fa_base, fr_base, "GET", "/hash/hlen?k=H",
                          seed=lambda: VERIFY.hset("H", mapping={"a": "1", "b": "2"}))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "HLEN", t_hlen)

    def t_hstrlen():
        fa, fr = both_req(fa_base, fr_base, "GET", "/hash/hstrlen?k=H&f=f",
                          seed=lambda: VERIFY.hset("H", "f", "hello"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "HSTRLEN", t_hstrlen)

    def t_hincrby():
        fa, fr = both_req(fa_base, fr_base, "POST", "/hash/hincrby?k=H&f=c&n=5")
        expect_equal(fa, fr, 200)
        assert VERIFY.hget("H", "c") == "5"
    tid = run_test(suite, tid, cat, "HINCRBY", t_hincrby)

    def t_hincrbyfloat():
        fa, fr = both_req(fa_base, fr_base, "POST", "/hash/hincrbyfloat?k=H&f=f&n=2.5")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "HINCRBYFLOAT", t_hincrbyfloat)

    def t_hscan():
        fa, fr = both_req(fa_base, fr_base, "GET", "/hash/hscan?k=H&match=*&count=4",
                          seed=lambda: VERIFY.hset("H", mapping={f"f{i}": str(i) for i in range(15)}))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "HSCAN", t_hscan)

    def t_hsetnx_new():
        fa, fr = both_req(fa_base, fr_base, "POST", "/hash/hsetnx?k=H&f=f&v=v")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "HSETNX new", t_hsetnx_new)

    def t_hsetnx_exists():
        fa, fr = both_req(fa_base, fr_base, "POST", "/hash/hsetnx?k=H&f=f&v=v",
                          seed=lambda: VERIFY.hset("H", "f", "pre"))
        expect_equal(fa, fr, 200)
        assert VERIFY.hget("H", "f") == "pre"
    tid = run_test(suite, tid, cat, "HSETNX exists", t_hsetnx_exists)

    # ── SETS ───────────────────────────────────────────────────
    cat = "sets"

    def t_sadd():
        fa, fr = both_req(fa_base, fr_base, "POST", "/set/sadd?k=S&m=a&m=b&m=c")
        expect_equal(fa, fr, 200)
        assert VERIFY.smembers("S") == {"a", "b", "c"}
    tid = run_test(suite, tid, cat, "SADD", t_sadd)

    def t_srem():
        fa, fr = both_req(fa_base, fr_base, "POST", "/set/srem?k=S&m=a&m=b",
                          seed=lambda: VERIFY.sadd("S", "a", "b", "c"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "SREM", t_srem)

    def t_smembers():
        fa, fr = both_req(fa_base, fr_base, "GET", "/set/smembers?k=S",
                          seed=lambda: VERIFY.sadd("S", "a", "b", "c"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "SMEMBERS", t_smembers)

    def t_scard():
        fa, fr = both_req(fa_base, fr_base, "GET", "/set/scard?k=S",
                          seed=lambda: VERIFY.sadd("S", "a", "b", "c"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "SCARD", t_scard)

    def t_sismember():
        fa, fr = both_req(fa_base, fr_base, "GET", "/set/sismember?k=S&m=a",
                          seed=lambda: VERIFY.sadd("S", "a", "b"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "SISMEMBER yes", t_sismember)

    def t_sismember_no():
        fa, fr = both_req(fa_base, fr_base, "GET", "/set/sismember?k=S&m=nope")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "SISMEMBER no", t_sismember_no)

    def t_smismember():
        fa, fr = both_req(fa_base, fr_base, "GET", "/set/smismember?k=S&m=a&m=c&m=b",
                          seed=lambda: VERIFY.sadd("S", "a", "b"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "SMISMEMBER", t_smismember)

    def _seed_ab():
        VERIFY.sadd("A", "a", "b"); VERIFY.sadd("B", "b", "c")

    def t_sunion():
        fa, fr = both_req(fa_base, fr_base, "GET", "/set/sunion?keys=A&keys=B",
                          seed=_seed_ab)
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "SUNION", t_sunion)

    def t_sinter():
        fa, fr = both_req(fa_base, fr_base, "GET", "/set/sinter?keys=A&keys=B",
                          seed=_seed_ab)
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "SINTER", t_sinter)

    def t_sdiff():
        fa, fr = both_req(fa_base, fr_base, "GET", "/set/sdiff?keys=A&keys=B",
                          seed=_seed_ab)
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "SDIFF", t_sdiff)

    def t_sunionstore():
        fa, fr = both_req(fa_base, fr_base, "POST", "/set/sunionstore?dst=D&keys=A&keys=B",
                          seed=_seed_ab)
        expect_equal(fa, fr, 200)
        assert VERIFY.smembers("D") == {"a", "b", "c"}
    tid = run_test(suite, tid, cat, "SUNIONSTORE", t_sunionstore)

    def t_sinterstore():
        fa, fr = both_req(fa_base, fr_base, "POST", "/set/sinterstore?dst=D&keys=A&keys=B",
                          seed=_seed_ab)
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "SINTERSTORE", t_sinterstore)

    def t_sdiffstore():
        fa, fr = both_req(fa_base, fr_base, "POST", "/set/sdiffstore?dst=D&keys=A&keys=B",
                          seed=_seed_ab)
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "SDIFFSTORE", t_sdiffstore)

    def t_spop():
        fa, fr = both_req(fa_base, fr_base, "POST", "/set/spop?k=S",
                          seed=lambda: VERIFY.sadd("S", "only"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "SPOP single", t_spop)

    def t_srandmember():
        fa, fr = both_req(fa_base, fr_base, "GET", "/set/srandmember?k=S&count=2",
                          seed=lambda: VERIFY.sadd("S", "a", "b", "c"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "SRANDMEMBER count=2", t_srandmember)

    def t_smove():
        fa, fr = both_req(fa_base, fr_base, "POST", "/set/smove?src=A&dst=B&m=a",
                          seed=lambda: (VERIFY.sadd("A", "a", "b"), VERIFY.sadd("B", "c")))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "SMOVE", t_smove)

    def t_sscan():
        fa, fr = both_req(fa_base, fr_base, "GET", "/set/sscan?k=S&match=*",
                          seed=lambda: VERIFY.sadd("S", *[f"m{i}" for i in range(12)]))
        expect_equal(fa, fr, 200, sort_list_keys=["members"])
    tid = run_test(suite, tid, cat, "SSCAN", t_sscan)

    # ── SORTED SETS ────────────────────────────────────────────
    cat = "zsets"

    def t_zadd():
        fa, fr = both_req(fa_base, fr_base, "POST", "/zset/zadd?k=Z",
                          json={"mapping": {"a": 1.0, "b": 2.0, "c": 3.0}})
        expect_equal(fa, fr, 200)
        assert VERIFY.zrange("Z", 0, -1, withscores=True) == [("a", 1.0), ("b", 2.0), ("c", 3.0)]
    tid = run_test(suite, tid, cat, "ZADD basic", t_zadd)

    def t_zadd_nx():
        fa, fr = both_req(fa_base, fr_base, "POST", "/zset/zadd?k=Z",
                          json={"mapping": {"a": 99.0, "b": 2.0}, "nx": True},
                          seed=lambda: VERIFY.zadd("Z", {"a": 1.0}))
        expect_equal(fa, fr, 200)
        assert VERIFY.zscore("Z", "a") == 1.0
        assert VERIFY.zscore("Z", "b") == 2.0
    tid = run_test(suite, tid, cat, "ZADD NX", t_zadd_nx)

    def t_zadd_xx():
        fa, fr = both_req(fa_base, fr_base, "POST", "/zset/zadd?k=Z",
                          json={"mapping": {"a": 99.0, "b": 2.0}, "xx": True},
                          seed=lambda: VERIFY.zadd("Z", {"a": 1.0}))
        expect_equal(fa, fr, 200)
        assert VERIFY.zscore("Z", "b") is None
    tid = run_test(suite, tid, cat, "ZADD XX", t_zadd_xx)

    def t_zadd_ch():
        fa, fr = both_req(fa_base, fr_base, "POST", "/zset/zadd?k=Z",
                          json={"mapping": {"a": 5.0, "c": 3.0}, "ch": True},
                          seed=lambda: VERIFY.zadd("Z", {"a": 1.0, "b": 2.0}))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "ZADD CH", t_zadd_ch)

    def t_zadd_incr():
        fa, fr = both_req(fa_base, fr_base, "POST", "/zset/zadd?k=Z",
                          json={"mapping": {"a": 2.5}, "incr": True},
                          seed=lambda: VERIFY.zadd("Z", {"a": 1.0}))
        expect_equal(fa, fr, 200)
        assert VERIFY.zscore("Z", "a") == 3.5
    tid = run_test(suite, tid, cat, "ZADD INCR", t_zadd_incr)

    def t_zadd_gt():
        fa, fr = both_req(fa_base, fr_base, "POST", "/zset/zadd?k=Z",
                          json={"mapping": {"a": 3.0}, "gt": True},
                          seed=lambda: VERIFY.zadd("Z", {"a": 5.0}))
        expect_equal(fa, fr, 200)
        assert VERIFY.zscore("Z", "a") == 5.0
    tid = run_test(suite, tid, cat, "ZADD GT keeps higher", t_zadd_gt)

    def t_zadd_lt():
        fa, fr = both_req(fa_base, fr_base, "POST", "/zset/zadd?k=Z",
                          json={"mapping": {"a": 3.0}, "lt": True},
                          seed=lambda: VERIFY.zadd("Z", {"a": 5.0}))
        expect_equal(fa, fr, 200)
        assert VERIFY.zscore("Z", "a") == 3.0
    tid = run_test(suite, tid, cat, "ZADD LT keeps lower", t_zadd_lt)

    def _seed_zabc():
        VERIFY.zadd("Z", {"a": 1.0, "b": 2.0, "c": 3.0})

    def t_zrange():
        fa, fr = both_req(fa_base, fr_base, "GET", "/zset/zrange?k=Z&start=0&stop=-1",
                          seed=_seed_zabc)
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "ZRANGE", t_zrange)

    def t_zrange_withscores():
        fa, fr = both_req(fa_base, fr_base, "GET", "/zset/zrange?k=Z&start=0&stop=-1&withscores=true",
                          seed=_seed_zabc)
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "ZRANGE withscores", t_zrange_withscores)

    def t_zrevrange():
        fa, fr = both_req(fa_base, fr_base, "GET", "/zset/zrevrange?k=Z&start=0&stop=-1",
                          seed=_seed_zabc)
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "ZREVRANGE", t_zrevrange)

    def t_zrangebyscore():
        fa, fr = both_req(fa_base, fr_base, "GET", "/zset/zrangebyscore?k=Z&min=2&max=3",
                          seed=lambda: VERIFY.zadd("Z", {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0}))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "ZRANGEBYSCORE", t_zrangebyscore)

    def t_zrangebylex():
        fa, fr = both_req(fa_base, fr_base, "GET", "/zset/zrangebylex?k=Z&min=[b&max=[c",
                          seed=lambda: VERIFY.zadd("Z", {"a": 0.0, "b": 0.0, "c": 0.0, "d": 0.0}))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "ZRANGEBYLEX", t_zrangebylex)

    def t_zscore():
        fa, fr = both_req(fa_base, fr_base, "GET", "/zset/zscore?k=Z&m=a",
                          seed=lambda: VERIFY.zadd("Z", {"a": 1.5}))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "ZSCORE", t_zscore)

    def t_zmscore():
        fa, fr = both_req(fa_base, fr_base, "GET", "/zset/zmscore?k=Z&m=a&m=nope&m=b",
                          seed=lambda: VERIFY.zadd("Z", {"a": 1.0, "b": 2.0}))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "ZMSCORE", t_zmscore)

    def t_zincrby():
        fa, fr = both_req(fa_base, fr_base, "POST", "/zset/zincrby?k=Z&m=a&by=2.5")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "ZINCRBY", t_zincrby)

    def t_zrank():
        fa, fr = both_req(fa_base, fr_base, "GET", "/zset/zrank?k=Z&m=b",
                          seed=_seed_zabc)
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "ZRANK", t_zrank)

    def t_zrevrank():
        fa, fr = both_req(fa_base, fr_base, "GET", "/zset/zrevrank?k=Z&m=a",
                          seed=_seed_zabc)
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "ZREVRANK", t_zrevrank)

    def t_zcard():
        fa, fr = both_req(fa_base, fr_base, "GET", "/zset/zcard?k=Z",
                          seed=lambda: VERIFY.zadd("Z", {"a": 1.0, "b": 2.0}))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "ZCARD", t_zcard)

    def t_zcount():
        fa, fr = both_req(fa_base, fr_base, "GET", "/zset/zcount?k=Z&min=2&max=3",
                          seed=_seed_zabc)
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "ZCOUNT", t_zcount)

    def t_zlexcount():
        fa, fr = both_req(fa_base, fr_base, "GET", "/zset/zlexcount?k=Z&min=-&max=%2B",
                          seed=lambda: VERIFY.zadd("Z", {"a": 0, "b": 0, "c": 0}))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "ZLEXCOUNT", t_zlexcount)

    def t_zpopmin():
        fa, fr = both_req(fa_base, fr_base, "POST", "/zset/zpopmin?k=Z&count=1",
                          seed=lambda: VERIFY.zadd("Z", {"a": 1.0, "b": 2.0}))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "ZPOPMIN", t_zpopmin)

    def t_zpopmax():
        fa, fr = both_req(fa_base, fr_base, "POST", "/zset/zpopmax?k=Z&count=1",
                          seed=lambda: VERIFY.zadd("Z", {"a": 1.0, "b": 2.0}))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "ZPOPMAX", t_zpopmax)

    def t_bzpopmin_timeout():
        fa, fr = both_req(fa_base, fr_base, "POST", "/zset/bzpopmin?keys=nope&timeout=0.15")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "BZPOPMIN timeout", t_bzpopmin_timeout)

    def t_bzpopmax_timeout():
        fa, fr = both_req(fa_base, fr_base, "POST", "/zset/bzpopmax?keys=nope&timeout=0.15")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "BZPOPMAX timeout", t_bzpopmax_timeout)

    def t_zrangestore():
        fa, fr = both_req(fa_base, fr_base, "POST", "/zset/zrangestore?dst=D&src=Z&start=0&stop=1",
                          seed=_seed_zabc)
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "ZRANGESTORE", t_zrangestore)

    def _seed_zabzbc():
        VERIFY.zadd("A", {"a": 1, "b": 2}); VERIFY.zadd("B", {"b": 3, "c": 4})

    def t_zunionstore():
        fa, fr = both_req(fa_base, fr_base, "POST", "/zset/zunionstore?dst=D&keys=A&keys=B",
                          seed=_seed_zabzbc)
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "ZUNIONSTORE", t_zunionstore)

    def t_zinterstore():
        fa, fr = both_req(fa_base, fr_base, "POST", "/zset/zinterstore?dst=D&keys=A&keys=B",
                          seed=_seed_zabzbc)
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "ZINTERSTORE", t_zinterstore)

    def t_zdiffstore():
        fa, fr = both_req(fa_base, fr_base, "POST", "/zset/zdiffstore?dst=D&keys=A&keys=B",
                          seed=lambda: (VERIFY.zadd("A", {"a": 1, "b": 2}), VERIFY.zadd("B", {"b": 3})))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "ZDIFFSTORE", t_zdiffstore)

    def t_zscan():
        fa, fr = both_req(fa_base, fr_base, "GET", "/zset/zscan?k=Z&match=*",
                          seed=lambda: VERIFY.zadd("Z", {f"m{i}": float(i) for i in range(12)}))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "ZSCAN", t_zscan)

    def t_zadd_inf():
        fa, fr = both_req(fa_base, fr_base, "POST", "/zset/zadd_inf?k=Z")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "ZADD with ±inf scores", t_zadd_inf)

    # ── PUB/SUB ────────────────────────────────────────────────
    cat = "pubsub"

    def t_pubsub_basic():
        # FA and FR have independent subscribers (sub_id_fa / sub_id_fr)
        sub_fa = f"fa-{int(time.time()*1000)}"
        sub_fr = f"fr-{int(time.time()*1000)+1}"
        _client(fa_base).post(f"/pubsub/subscribe_start?sub_id={sub_fa}&c=ch1&duration_ms=400")
        _client(fr_base).post(f"/pubsub/subscribe_start?sub_id={sub_fr}&c=ch1&duration_ms=400")
        # Publish 3 messages via both servers; each subscriber sees all 6.
        for m in ("hi1", "hi2", "hi3"):
            _client(fa_base).post(f"/pubsub/publish?channel=ch1&msg={m}")
        for m in ("hi4", "hi5", "hi6"):
            _client(fr_base).post(f"/pubsub/publish?channel=ch1&msg={m}")
        fa = _client(fa_base).get(f"/pubsub/subscribe_result?sub_id={sub_fa}&wait_ms=700")
        fr = _client(fr_base).get(f"/pubsub/subscribe_result?sub_id={sub_fr}&wait_ms=700")
        assert fa.status_code == 200 and fr.status_code == 200, f"statuses {fa.status_code} {fr.status_code}"
        ja, jr = fa.json(), fr.json()
        assert ja.get("count") == 6 and jr.get("count") == 6, f"counts: fa={ja.get('count')} fr={jr.get('count')}"
    tid = run_test(suite, tid, cat, "PUBSUB subscribe + 6 msgs", t_pubsub_basic, flush=True)

    def t_pubsub_pattern():
        sub_fa = f"fa-p-{int(time.time()*1000)}"
        sub_fr = f"fr-p-{int(time.time()*1000)+1}"
        _client(fa_base).post(f"/pubsub/subscribe_start?sub_id={sub_fa}&p=news.*&duration_ms=400")
        _client(fr_base).post(f"/pubsub/subscribe_start?sub_id={sub_fr}&p=news.*&duration_ms=400")
        _client(fa_base).post("/pubsub/publish?channel=news.tech&msg=ai")
        _client(fr_base).post("/pubsub/publish?channel=news.biz&msg=market")
        fa = _client(fa_base).get(f"/pubsub/subscribe_result?sub_id={sub_fa}&wait_ms=600")
        fr = _client(fr_base).get(f"/pubsub/subscribe_result?sub_id={sub_fr}&wait_ms=600")
        assert fa.status_code == 200 and fr.status_code == 200
        assert fa.json()["count"] == 2 and fr.json()["count"] == 2
    tid = run_test(suite, tid, cat, "PSUBSCRIBE news.*", t_pubsub_pattern, flush=True)

    def t_publish_no_subs():
        fa, fr = both_req(fa_base, fr_base, "POST", "/pubsub/publish?channel=no_listeners&msg=void")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "PUBLISH to no subs", t_publish_no_subs)

    # ── PIPELINES ──────────────────────────────────────────────
    cat = "pipelines"

    def t_pipe_simple():
        fa, fr = both_req(fa_base, fr_base, "POST", "/pipe/simple?key_prefix=P1")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "pipeline SET+INCR", t_pipe_simple)

    def t_pipe_txn():
        fa, fr = both_req(fa_base, fr_base, "POST", "/pipe/transaction?key=T1")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "transaction pipeline MULTI/EXEC", t_pipe_txn)

    def t_pipe_watch():
        fa, fr = both_req(fa_base, fr_base, "POST", "/pipe/watch?key=W1")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "WATCH/MULTI/EXEC", t_pipe_watch)

    def t_pipe_mixed():
        fa, fr = both_req(fa_base, fr_base, "POST", "/pipe/mixed?ns=Mix1")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "mixed pipeline", t_pipe_mixed)

    # ── STREAMS ────────────────────────────────────────────────
    cat = "streams"

    def t_xadd_xlen():
        fa, fr = both_req(fa_base, fr_base, "POST", "/stream/xadd?k=ST", json={"a": "1", "b": "2"})
        expect_equal(fa, fr, 200, compare_body=False)  # IDs differ between runs
        # Seed before checking XLEN parity
        fa2, fr2 = both_req(fa_base, fr_base, "GET", "/stream/xlen?k=ST",
                            seed=lambda: VERIFY.xadd("ST", {"a": "1"}))
        expect_equal(fa2, fr2, 200)
    tid = run_test(suite, tid, cat, "XADD + XLEN", t_xadd_xlen)

    def t_xadd_nomkstream():
        fa, fr = both_req(fa_base, fr_base, "POST", "/stream/xadd?k=NEW&nomkstream=true",
                          json={"a": "1"})
        expect_equal(fa, fr, 200, compare_body=False)
    tid = run_test(suite, tid, cat, "XADD NOMKSTREAM", t_xadd_nomkstream)

    def t_xadd_maxlen():
        # redis-py's xadd(..., maxlen=N) defaults to approximate=True, so
        # small streams may not be trimmed exactly. Both FA and FR call the
        # same redis-py code path, so we verify identical approximate
        # behaviour rather than an exact length.
        flushdb()
        for _ in range(5):
            r = _client(fa_base).post("/stream/xadd?k=ST&maxlen=3", json={"i": "x"})
            assert r.status_code == 200
        fa_len = VERIFY.xlen("ST")
        flushdb()
        for _ in range(5):
            r = _client(fr_base).post("/stream/xadd?k=ST&maxlen=3", json={"i": "x"})
            assert r.status_code == 200
        fr_len = VERIFY.xlen("ST")
        assert fa_len == fr_len, f"FA xlen={fa_len} FR xlen={fr_len}"
    tid = run_test(suite, tid, cat, "XADD MAXLEN", t_xadd_maxlen, flush=True)

    def _seed_st_two():
        VERIFY.xadd("ST", {"f": "v1"}); VERIFY.xadd("ST", {"f": "v2"})

    def t_xrange():
        fa, fr = both_req(fa_base, fr_base, "GET", "/stream/xrange?k=ST",
                          seed=_seed_st_two)
        # Can't compare IDs, but structure should match
        expect_equal(fa, fr, 200, compare_body=False)
        ja, jr = r_json(fa), r_json(fr)
        assert len(ja["items"]) == len(jr["items"]) == 2
        for (a_id, a_fields), (b_id, b_fields) in zip(ja["items"], jr["items"]):
            assert a_fields == b_fields
    tid = run_test(suite, tid, cat, "XRANGE", t_xrange)

    def t_xrevrange():
        fa, fr = both_req(fa_base, fr_base, "GET", "/stream/xrevrange?k=ST",
                          seed=_seed_st_two)
        expect_equal(fa, fr, 200, compare_body=False)
        ja, jr = r_json(fa), r_json(fr)
        assert len(ja["items"]) == len(jr["items"]) == 2
    tid = run_test(suite, tid, cat, "XREVRANGE", t_xrevrange)

    def t_xread():
        fa, fr = both_req(fa_base, fr_base, "GET", "/stream/xread?k=ST&start=0&count=10",
                          seed=_seed_st_two)
        expect_equal(fa, fr, 200, compare_body=False)
        ja, jr = r_json(fa), r_json(fr)
        assert len(ja["entries"]) == len(jr["entries"]) == 2
    tid = run_test(suite, tid, cat, "XREAD", t_xread)

    def t_xgroup():
        fa, fr = both_req(fa_base, fr_base, "POST", "/stream/xgroup_create?k=ST&group=g1&id=0&mkstream=true",
                          seed=lambda: VERIFY.xadd("ST", {"f": "v"}))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "XGROUP CREATE", t_xgroup)

    def _seed_grp():
        VERIFY.xadd("ST", {"f": "v"})
        VERIFY.xgroup_create("ST", "g1", id=0, mkstream=True)
        VERIFY.xadd("ST", {"f": "v2"})

    def t_xreadgroup():
        fa, fr = both_req(fa_base, fr_base, "POST", "/stream/xreadgroup?k=ST&group=g1&consumer=c1&count=10",
                          seed=_seed_grp)
        expect_equal(fa, fr, 200, compare_body=False)
        ja, jr = r_json(fa), r_json(fr)
        assert len(ja["entries"]) == len(jr["entries"])
    tid = run_test(suite, tid, cat, "XREADGROUP", t_xreadgroup)

    def t_xack_xpending():
        # Build setup then have the server consume+ack
        from urllib.parse import urlencode

        def setup():
            VERIFY.xadd("ST", {"f": "v"})
            VERIFY.xgroup_create("ST", "g1", id=0, mkstream=True)
            VERIFY.xadd("ST", {"f": "v2"})

        def ack_query(ids):
            # `ids: list[str] = Query()` needs repeated `ids=` params,
            # not a comma-joined string (xack treats the whole string as
            # one id and Redis rejects it → 500).
            return urlencode([("ids", i) for i in ids])

        flushdb(); setup()
        ids_fa = _client(fa_base).post("/stream/xreadgroup?k=ST&group=g1&consumer=c1&count=10").json()
        read_ids = [e[1] for e in ids_fa["entries"]]
        fa_ack = _client(fa_base).post(f"/stream/xack?k=ST&group=g1&{ack_query(read_ids)}") if read_ids else None
        flushdb(); setup()
        ids_fr = _client(fr_base).post("/stream/xreadgroup?k=ST&group=g1&consumer=c1&count=10").json()
        read_ids_fr = [e[1] for e in ids_fr["entries"]]
        fr_ack = _client(fr_base).post(f"/stream/xack?k=ST&group=g1&{ack_query(read_ids_fr)}") if read_ids_fr else None
        if fa_ack and fr_ack:
            expect_equal(fa_ack, fr_ack, 200)
    tid = run_test(suite, tid, cat, "XACK + XPENDING", t_xack_xpending)

    def t_xtrim():
        def _seed():
            for _ in range(6):
                VERIFY.xadd("ST", {"f": "v"})
        fa, fr = both_req(fa_base, fr_base, "POST", "/stream/xtrim?k=ST&maxlen=2", seed=_seed)
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "XTRIM", t_xtrim)

    def t_xdel():
        # Can't deterministically generate same IDs, so just check status
        flushdb()
        mid_fa = VERIFY.xadd("ST", {"f": "v"})
        fa_r = _client(fa_base).post(f"/stream/xdel?k=ST&ids={mid_fa}")
        flushdb()
        mid_fr = VERIFY.xadd("ST", {"f": "v"})
        fr_r = _client(fr_base).post(f"/stream/xdel?k=ST&ids={mid_fr}")
        expect_equal(fa_r, fr_r, 200)
    tid = run_test(suite, tid, cat, "XDEL", t_xdel)

    # ── LUA ────────────────────────────────────────────────────
    cat = "lua"

    def t_lua_eval():
        fa, fr = both_req(fa_base, fr_base, "POST", "/lua/eval",
                          json={"script": "return 42", "keys": [], "args": []})
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "EVAL return 42", t_lua_eval)

    def t_lua_eval_keys_args():
        fa, fr = both_req(fa_base, fr_base, "POST", "/lua/eval", json={
            "script": "return redis.call('SET', KEYS[1], ARGV[1])",
            "keys": ["k"], "args": ["v"],
        })
        expect_equal(fa, fr, 200)
        assert VERIFY.get("k") == "v"
    tid = run_test(suite, tid, cat, "EVAL SET via KEYS/ARGV", t_lua_eval_keys_args)

    def t_lua_evalsha():
        fa, fr = both_req(fa_base, fr_base, "POST", "/lua/load_and_evalsha", json={
            "script": "return 123",
            "keys": [], "args": [],
        })
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "SCRIPT LOAD + EVALSHA", t_lua_evalsha)

    def t_lua_incr_if_eq_match():
        # Script increments when stored value equals expected. Redis INCR
        # only works on numeric strings, so seed a numeric value.
        fa, fr = both_req(fa_base, fr_base, "POST", "/lua/incr_if_eq?k=k&expected=10",
                          seed=lambda: VERIFY.set("k", "10"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "EVAL atomic incr-if-eq", t_lua_incr_if_eq_match)

    # ── HIGH-LEVEL PATTERNS ────────────────────────────────────
    cat = "patterns"

    def t_cache_miss():
        fa, fr = both_req(fa_base, fr_base, "GET", "/app/cache/hello?name=world")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "cache/hello — miss", t_cache_miss)

    def t_cache_hit():
        fa, fr = both_req(fa_base, fr_base, "GET", "/app/cache/hello?name=x",
                          seed=lambda: VERIFY.setex("cache:hello:x", 60, "hello, x"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "cache/hello — warm hit", t_cache_hit)

    def t_ratelimit_ok():
        fa, fr = both_req(fa_base, fr_base, "POST", "/app/ratelimit?user=uA&limit=10&window=60")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "ratelimit under limit", t_ratelimit_ok)

    def t_ratelimit_exceeded():
        def _seed():
            VERIFY.set("rl:uB", "99"); VERIFY.expire("rl:uB", 60)
        fa, fr = both_req(fa_base, fr_base, "POST", "/app/ratelimit?user=uB&limit=3&window=60",
                          seed=_seed)
        expect_equal(fa, fr, 429, compare_body=False)
    tid = run_test(suite, tid, cat, "ratelimit 429 when exceeded", t_ratelimit_exceeded)

    def t_session_create():
        fa, fr = both_req(fa_base, fr_base, "POST", "/app/session/create?user=u1")
        # session_id has timestamp, so differs. Compare statuses only.
        expect_equal(fa, fr, 200, compare_body=False)
    tid = run_test(suite, tid, cat, "session create", t_session_create)

    def t_session_404():
        fa, fr = both_req(fa_base, fr_base, "GET", "/app/session/get?sid=nope")
        expect_equal(fa, fr, 404, compare_body=False)
    tid = run_test(suite, tid, cat, "session missing → 404", t_session_404)

    def t_flags():
        fa, fr = both_req(fa_base, fr_base, "POST", "/app/flags/set",
                          json={"signup": "on", "ai": "off"})
        expect_equal(fa, fr, 200)
        fa2, fr2 = both_req(fa_base, fr_base, "GET", "/app/flags/all",
                            seed=lambda: VERIFY.hset("flags", mapping={"signup": "on", "ai": "off"}))
        expect_equal(fa2, fr2, 200)
    tid = run_test(suite, tid, cat, "feature flags", t_flags)

    def t_flag_one():
        fa, fr = both_req(fa_base, fr_base, "GET", "/app/flags/one?name=x",
                          seed=lambda: VERIFY.hset("flags", "x", "on"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "feature flag single", t_flag_one)

    def t_leaderboard():
        def _seed():
            VERIFY.zadd("leaderboard", {"alice": 100, "bob": 200, "carol": 150})
        fa, fr = both_req(fa_base, fr_base, "GET", "/app/lb/top?n=3", seed=_seed)
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "leaderboard top-3", t_leaderboard)

    def t_lock_acquire_free():
        fa, fr = both_req(fa_base, fr_base, "POST", "/app/lock/acquire?name=L1&ttl_ms=2000")
        # Both get acquired=True (fresh DB each time). But "token" has timestamp.
        expect_equal(fa, fr, 200, compare_body=False)
        ja, jr = r_json(fa), r_json(fr)
        assert ja["acquired"] == jr["acquired"] == True
    tid = run_test(suite, tid, cat, "distributed lock (free)", t_lock_acquire_free)

    def t_lock_acquire_held():
        fa, fr = both_req(fa_base, fr_base, "POST", "/app/lock/acquire?name=L2&ttl_ms=2000",
                          seed=lambda: VERIFY.set("lock:L2", "other"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "distributed lock (held)", t_lock_acquire_held)

    def t_lock_release():
        fa, fr = both_req(fa_base, fr_base, "POST", "/app/lock/release?name=L2&token=my-token",
                          seed=lambda: VERIFY.set("lock:L2", "my-token"))
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "lock release", t_lock_release)

    # ── SERIALIZATION ──────────────────────────────────────────
    cat = "serialization"

    def t_utf8():
        fa, fr = both_req(fa_base, fr_base, "POST", "/ser/utf8?k=u&v=hello")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "utf8 roundtrip", t_utf8)

    def t_unicode():
        fa, fr = both_req(fa_base, fr_base, "POST", "/ser/unicode?k=u")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "unicode (emoji/CJK)", t_unicode)

    def t_json_payload():
        fa, fr = both_req(fa_base, fr_base, "POST", "/ser/json?k=j",
                          json={"payload": {"a": 1, "b": [1, 2, 3], "c": {"d": "e"}}})
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "json payload roundtrip", t_json_payload)

    def t_bytes():
        fa, fr = both_req(fa_base, fr_base, "POST", "/ser/bytes?k=b&v=binary-data")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "bytes roundtrip (raw pool)", t_bytes)

    # ── ERROR HANDLING ─────────────────────────────────────────
    cat = "errors"

    def t_wrongtype():
        fa, fr = both_req(fa_base, fr_base, "GET", "/err/wrongtype?k=W")
        expect_equal(fa, fr, 409, compare_body=False)
    tid = run_test(suite, tid, cat, "WRONGTYPE mapped to 409", t_wrongtype)

    def t_nonexistent_get():
        fa, fr = both_req(fa_base, fr_base, "GET", "/err/nonexistent_get")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "GET missing → null", t_nonexistent_get)

    def t_type_missing():
        fa, fr = both_req(fa_base, fr_base, "GET", "/err/type_nonexistent")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "TYPE missing → 'none'", t_type_missing)

    def t_ttl_missing():
        fa, fr = both_req(fa_base, fr_base, "GET", "/err/ttl_nonexistent")
        expect_equal(fa, fr, 200)
    tid = run_test(suite, tid, cat, "TTL missing → -2", t_ttl_missing)

    # ── PARAMETRIC fuzz (bunches) ──────────────────────────────
    cat = "parametric-strings"
    for i in range(20):
        def t(i=i):
            k = f"pk{i}"
            fa, fr = both_req(fa_base, fr_base, "POST", f"/str/set?k={k}", json={"value": f"val-{i}"})
            expect_equal(fa, fr, 200)
            assert VERIFY.get(k) == f"val-{i}"
        tid = run_test(suite, tid, cat, f"SET pk{i}", t)

    cat = "parametric-incr"
    for i in range(15):
        def t(i=i):
            # Each incr is isolated (fresh DB) so result is always i+1
            fa, fr = both_req(fa_base, fr_base, "POST", f"/str/incrby?k=cnt&n={i+1}")
            expect_equal(fa, fr, 200)
        tid = run_test(suite, tid, cat, f"INCRBY cnt n={i+1}", t)

    cat = "parametric-hash"
    for i in range(15):
        def t(i=i):
            fa, fr = both_req(fa_base, fr_base, "POST", f"/hash/hset?k=H{i}",
                              json={"a": str(i), "b": str(i*2)})
            expect_equal(fa, fr, 200)
            assert VERIFY.hget(f"H{i}", "a") == str(i)
        tid = run_test(suite, tid, cat, f"HSET H{i}", t)

    cat = "parametric-zset"
    for i in range(15):
        def t(i=i):
            fa, fr = both_req(fa_base, fr_base, "POST", f"/zset/zadd?k=Z{i}",
                              json={"mapping": {f"m{i}": float(i), "x": 0.0}})
            expect_equal(fa, fr, 200)
        tid = run_test(suite, tid, cat, f"ZADD Z{i}", t)

    cat = "parametric-list"
    for i in range(15):
        def t(i=i):
            fa, fr = both_req(fa_base, fr_base, "POST", f"/list/rpush?k=L{i}&v=a&v=b&v=c")
            expect_equal(fa, fr, 200)
            assert VERIFY.lrange(f"L{i}", 0, -1) == ["a", "b", "c"]
        tid = run_test(suite, tid, cat, f"RPUSH L{i}", t)

    cat = "parametric-set"
    for i in range(15):
        def t(i=i):
            fa, fr = both_req(fa_base, fr_base, "POST", f"/set/sadd?k=S{i}&m=a&m=b")
            expect_equal(fa, fr, 200)
            assert VERIFY.smembers(f"S{i}") == {"a", "b"}
        tid = run_test(suite, tid, cat, f"SADD S{i}", t)

    cat = "parametric-json"
    for i in range(10):
        def t(i=i):
            fa, fr = both_req(fa_base, fr_base, "POST", f"/ser/json?k=j{i}",
                              json={"payload": {"i": i, "list": list(range(i))}})
            expect_equal(fa, fr, 200)
        tid = run_test(suite, tid, cat, f"json roundtrip j{i}", t)

    cat = "parametric-pipe"
    for i in range(8):
        def t(i=i):
            fa, fr = both_req(fa_base, fr_base, "POST", f"/pipe/simple?key_prefix=P{i}")
            expect_equal(fa, fr, 200)
        tid = run_test(suite, tid, cat, f"pipe P{i}", t)


# ── Main runner ────────────────────────────────────────────────────

def boot_pair(app_module: str, app_import: str, fa_port: int, fr_port: int):
    print(f"Starting uvicorn ({app_module}) on :{fa_port}…")
    fa = start_uvicorn(fa_port, app_module)
    print(f"Starting fastapi-turbo ({app_import}) on :{fr_port}…")
    fr = start_fastapi_turbo(fr_port, app_import)
    if not wait_for_port(fa_port):
        try:
            out = fa.stderr.read(2000).decode(errors="ignore")
        except Exception:
            out = ""
        print(f"{RED}uvicorn failed to start on :{fa_port}{RESET}\n{out}")
        return fa, fr, False
    if not wait_for_port(fr_port):
        try:
            out = fr.stderr.read(2000).decode(errors="ignore")
        except Exception:
            out = ""
        print(f"{RED}fastapi-turbo failed to start on :{fr_port}{RESET}\n{out}")
        return fa, fr, False
    # Verify /health on both
    for base in [f"http://{HOST}:{fa_port}", f"http://{HOST}:{fr_port}"]:
        try:
            r = httpx.get(base + "/health", timeout=3.0)
            if r.status_code != 200:
                print(f"{RED}health failed on {base}: {r.status_code} {r.text[:200]}{RESET}")
        except Exception as e:
            print(f"{RED}health probe failed on {base}: {e}{RESET}")
    return fa, fr, True


def main():
    print(f"\n{BOLD}{'='*72}")
    print(f"  Redis Deep Integration Parity Suite")
    print(f"  Redis :{REDIS_PORT}")
    print(f"  Sync  FA:{FA_SYNC_PORT}  FR:{FR_SYNC_PORT}")
    print(f"  Async FA:{FA_ASYNC_PORT} FR:{FR_ASYNC_PORT}")
    print(f"{'='*72}{RESET}\n")

    ensure_redis()

    rc = 0
    suites: list[Suite] = []

    # ── SYNC suite ─────────────────────────────────────────────
    procs = []
    try:
        fa, fr, ok = boot_pair(
            "tests.parity.parity_app_redis:app",
            "tests.parity.parity_app_redis",
            FA_SYNC_PORT, FR_SYNC_PORT,
        )
        procs = [fa, fr]
        if not ok:
            return 1
        time.sleep(0.3)
        print(f"{GREEN}Sync servers ready.{RESET}\n")
        fa_base = f"http://{HOST}:{FA_SYNC_PORT}"
        fr_base = f"http://{HOST}:{FR_SYNC_PORT}"
        s = Suite("SYNC")
        t0 = time.time()
        run_suite(s, fa_base, fr_base)
        elapsed = time.time() - t0
        suites.append(s)
        total = len(s.results); passed = sum(1 for r in s.results if r[3])
        print(f"\n{BOLD}SYNC: {total} tests | {GREEN}{passed} PASS{RESET}{BOLD} | {RED}{total - passed} FAIL{RESET}{BOLD} | {elapsed:.1f}s{RESET}\n")
    except Exception as e:
        print(f"{RED}Sync suite crashed: {e}{RESET}")
        traceback.print_exc()
        rc = 1
    finally:
        close_clients()
        for p in procs:
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except Exception:
                    p.kill()

    # Give ports time to free up
    time.sleep(1.0)
    _reset_circuit()

    # ── ASYNC suite ────────────────────────────────────────────
    procs = []
    try:
        fa, fr, ok = boot_pair(
            "tests.parity.parity_app_redis_async:app",
            "tests.parity.parity_app_redis_async",
            FA_ASYNC_PORT, FR_ASYNC_PORT,
        )
        procs = [fa, fr]
        if not ok:
            return 1
        time.sleep(0.3)
        print(f"{GREEN}Async servers ready.{RESET}\n")
        fa_base = f"http://{HOST}:{FA_ASYNC_PORT}"
        fr_base = f"http://{HOST}:{FR_ASYNC_PORT}"
        s = Suite("ASYNC")
        t0 = time.time()
        run_suite(s, fa_base, fr_base)
        elapsed = time.time() - t0
        suites.append(s)
        total = len(s.results); passed = sum(1 for r in s.results if r[3])
        print(f"\n{BOLD}ASYNC: {total} tests | {GREEN}{passed} PASS{RESET}{BOLD} | {RED}{total - passed} FAIL{RESET}{BOLD} | {elapsed:.1f}s{RESET}\n")

        # Async-specific cancellation tests
        s_async = Suite("ASYNC-CANCEL")
        suites.append(s_async)
        tid = 1
        def t_cancel_blpop():
            fa, fr = both_req(fa_base, fr_base, "POST", "/list/cancel_blpop?k=nope&timeout=2.0")
            expect_equal(fa, fr, 200, compare_body=False)
        tid = run_test(s_async, tid, "cancel", "async blpop cancellation", t_cancel_blpop)

        def t_cancel_pubsub_get():
            fa, fr = both_req(fa_base, fr_base, "POST", "/pubsub/cancel_get_message")
            expect_equal(fa, fr, 200, compare_body=False)
        tid = run_test(s_async, tid, "cancel", "async pubsub.get_message cancellation", t_cancel_pubsub_get)

    except Exception as e:
        print(f"{RED}Async suite crashed: {e}{RESET}")
        traceback.print_exc()
        rc = 1
    finally:
        close_clients()
        for p in procs:
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except Exception:
                    p.kill()

    # ── Final report ───────────────────────────────────────────
    print(f"\n{BOLD}{'='*72}")
    print(f"  OVERALL RESULTS")
    print(f"{'='*72}{RESET}")

    grand_total = 0; grand_passed = 0
    for s in suites:
        tot = len(s.results); p = sum(1 for r in s.results if r[3])
        grand_total += tot; grand_passed += p
        pct = 100.0 * p / tot if tot else 0.0
        color = GREEN if p == tot else (YELLOW if p > 0.8 * tot else RED)
        print(f"  {color}{s.label:15s}{RESET} {p:4d}/{tot:4d}  ({pct:5.1f}%)")

    pct = 100.0 * grand_passed / grand_total if grand_total else 0.0
    color = GREEN if grand_passed == grand_total else (YELLOW if pct > 85 else RED)
    print(f"  {BOLD}{'TOTAL':15s}{RESET} {color}{grand_passed:4d}/{grand_total:4d}  ({pct:5.1f}%){RESET}\n")

    # Per-category breakdown across both suites
    for s in suites:
        by_cat: dict[str, list] = defaultdict(list)
        for tid, cat, desc, passed, detail in s.results:
            by_cat[cat].append((tid, desc, passed, detail))
        print(f"\n{BOLD}{s.label} category breakdown:{RESET}")
        cat_fails: list[tuple[str, int]] = []
        for cat in sorted(by_cat):
            rows = by_cat[cat]
            cp = sum(1 for r in rows if r[2])
            cf = len(rows) - cp
            color = GREEN if cf == 0 else RED
            marker = "" if cf == 0 else f"  ({cf} failed)"
            print(f"  {color}{cat:28s}{RESET} {cp:4d}/{len(rows):4d}{marker}")
            if cf:
                cat_fails.append((cat, cf))

    # Top divergent patterns (most common failure details)
    from collections import Counter
    all_fails: list[tuple[str, str, str, str]] = []
    for s in suites:
        for tid, cat, desc, passed, detail in s.results:
            if not passed:
                all_fails.append((s.label, cat, desc, detail))

    if all_fails:
        print(f"\n{BOLD}Top 20 divergent patterns:{RESET}")
        # Group by "cat + first 80 chars of detail"
        signatures = Counter()
        examples: dict[str, tuple[str, str, str]] = {}
        for label, cat, desc, detail in all_fails:
            sig_detail = detail.split("\n", 1)[0][:120]
            sig = f"{label}/{cat}:{sig_detail}"
            signatures[sig] += 1
            examples.setdefault(sig, (label, cat, f"{desc} — {detail[:200]}"))
        for sig, count in signatures.most_common(20):
            label, cat, exm = examples[sig]
            print(f"  {RED}[{label}/{cat}] x{count}{RESET}  {exm}")

    print(f"\n{BOLD}Final parity score: {color}{grand_passed}/{grand_total} = {pct:.1f}%{RESET}\n")

    return rc


if __name__ == "__main__":
    sys.exit(main())
