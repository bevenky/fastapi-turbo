#!/usr/bin/env python3
"""Deep SQLAlchemy parity runner: psycopg3 + asyncpg + psycopg2.

Per driver:
  - Boots stock FastAPI (uvicorn) on port 29930/29940/29950
  - Boots fastapi-rs on 29931/29941/29951
  - Resets DB, then runs ~150 behavior tests comparing both responses
  - After every test, DB-state is verified via an independent connection

Usage:
    python tests/parity/run_sqla_parity.py [--driver pg3|async|pg2|all]
                                           [--stop-on-fail]
                                           [--verbose]
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import traceback
from collections import defaultdict
from dataclasses import dataclass, field

import httpx


HOST = "127.0.0.1"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

FASTAPI_PORTS = {"pg3": 29930, "async": 29940, "pg2": 29950}
FASTAPI_RS_PORTS = {"pg3": 29931, "async": 29941, "pg2": 29951}

APP_MODULE = {
    "pg3":   "tests.parity.parity_app_sqla_psycopg3:app",
    "async": "tests.parity.parity_app_sqla_asyncpg:app",
    "pg2":   "tests.parity.parity_app_sqla_psycopg2:app",
}

# Each driver uses two DBs: one for FastAPI/uvicorn, one for fastapi-rs.
FA_DB_URL = {
    "pg3":   "postgresql+psycopg://venky@localhost:5432/jamun_sqla_pg3_fa",
    "async": "postgresql+asyncpg://venky@localhost:5432/jamun_sqla_async_fa",
    "pg2":   "postgresql+psycopg2://venky@localhost:5432/jamun_sqla_pg2_fa",
}
FR_DB_URL = {
    "pg3":   "postgresql+psycopg://venky@localhost:5432/jamun_sqla_pg3_fr",
    "async": "postgresql+asyncpg://venky@localhost:5432/jamun_sqla_async_fr",
    "pg2":   "postgresql+psycopg2://venky@localhost:5432/jamun_sqla_pg2_fr",
}
# Used by the post-hoc DB verifier (plain psycopg, always).
FA_VERIFY_URL = {
    "pg3":   "postgresql://venky@localhost:5432/jamun_sqla_pg3_fa",
    "async": "postgresql://venky@localhost:5432/jamun_sqla_async_fa",
    "pg2":   "postgresql://venky@localhost:5432/jamun_sqla_pg2_fa",
}
FR_VERIFY_URL = {
    "pg3":   "postgresql://venky@localhost:5432/jamun_sqla_pg3_fr",
    "async": "postgresql://venky@localhost:5432/jamun_sqla_async_fr",
    "pg2":   "postgresql://venky@localhost:5432/jamun_sqla_pg2_fr",
}
# Map driver → URL env var name that the app module reads
URL_ENV = {"pg3": "SQLA_URL_PG3", "async": "SQLA_URL_ASYNC", "pg2": "SQLA_URL_PG2"}


# ───────── helpers ───────────────────────────────────────────────────

def wait_for_port(port, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, port), timeout=1):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.2)
    return False


def wait_for_health(port, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://{HOST}:{port}/health", timeout=1.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def start_uvicorn(driver, port):
    env = os.environ.copy()
    env["PYTHONPATH"] = PROJECT_ROOT
    env["FASTAPI_RS_NO_SHIM"] = "1"
    env[URL_ENV[driver]] = FA_DB_URL[driver]
    # Discard stdout to avoid pipe-buffer deadlock when server logs > 64KiB.
    # Keep stderr in a tempfile for post-mortem inspection.
    err_path = f"/tmp/jamun_sqla_fa_{driver}.err"
    err_fh = open(err_path, "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", APP_MODULE[driver],
         "--host", HOST, "--port", str(port), "--log-level", "error"],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=err_fh,
    )
    proc._err_path = err_path  # type: ignore
    return proc


def start_fastapi_rs(driver, port):
    env = os.environ.copy()
    env["PYTHONPATH"] = PROJECT_ROOT
    env[URL_ENV[driver]] = FR_DB_URL[driver]
    module = APP_MODULE[driver].split(":")[0]
    script = f"""
import sys
sys.path.insert(0, {PROJECT_ROOT!r})
import fastapi_rs.compat
fastapi_rs.compat.install()
from {module} import app
app.run(host={HOST!r}, port={port})
"""
    err_path = f"/tmp/jamun_sqla_fr_{driver}.err"
    err_fh = open(err_path, "wb")
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=err_fh,
    )
    proc._err_path = err_path  # type: ignore
    return proc


def ensure_postgres() -> bool:
    """Return True if postgres is reachable, False if we should bail."""
    try:
        import psycopg
        c = psycopg.connect("postgresql://venky@localhost:5432/postgres", connect_timeout=3)
        c.close()
        return True
    except Exception as e:
        print(f"{RED}postgres not reachable: {e}{RESET}")
        return False


# ───────── test result tracking ──────────────────────────────────────

@dataclass
class Results:
    total: int = 0
    passed: int = 0
    failed: int = 0
    by_category: dict = field(default_factory=lambda: defaultdict(lambda: [0, 0]))
    failures: list = field(default_factory=list)

    def record(self, cat: str, tid: str, ok: bool, detail: str = ""):
        self.total += 1
        self.by_category[cat][1] += 1
        if ok:
            self.passed += 1
            self.by_category[cat][0] += 1
        else:
            self.failed += 1
            self.failures.append((cat, tid, detail))


# ───────── test harness ──────────────────────────────────────────────

class Harness:
    def __init__(self, fa_port, fr_port, driver, verbose=False):
        self.fa = httpx.Client(base_url=f"http://{HOST}:{fa_port}", timeout=10.0)
        self.fr = httpx.Client(base_url=f"http://{HOST}:{fr_port}", timeout=10.0)
        self.driver = driver
        self.verbose = verbose
        self.results = Results()

    def close(self):
        self.fa.close()
        self.fr.close()

    def _both(self, method, path, **kw):
        fa = self._safe(lambda: self.fa.request(method, path, **kw))
        fr = self._safe(lambda: self.fr.request(method, path, **kw))
        return fa, fr

    @staticmethod
    def _safe(fn):
        try:
            return fn()
        except Exception as e:
            return e

    def reset(self):
        try:
            self.fa.get("/__reset")
        except Exception:
            pass
        try:
            self.fr.get("/__reset")
        except Exception:
            pass

    def check(self, cat: str, tid: str, method: str, path: str, **kw):
        """Issue same request to both, compare status + json body.

        Supports extra kwargs:
          - expect_status=<int>   enforce a specific status
          - compare='json'|'keys'|'status'|'text'
        """
        compare = kw.pop("compare", "json")
        expect_status = kw.pop("expect_status", None)
        fa, fr = self._both(method, path, **kw)
        ok, detail = self._compare(fa, fr, compare, expect_status)
        self.results.record(cat, tid, ok, detail)
        if not ok and self.verbose:
            print(f"  {RED}FAIL{RESET} [{cat}] {tid} {method} {path}: {detail[:200]}")
        return fa, fr

    def _compare(self, fa, fr, compare, expect_status):
        if isinstance(fa, Exception) or isinstance(fr, Exception):
            return False, f"exception fa={type(fa).__name__ if isinstance(fa, Exception) else 'ok'} fr={type(fr).__name__ if isinstance(fr, Exception) else 'ok'}: fa={fa} fr={fr}"
        if fa.status_code != fr.status_code:
            return False, f"status: fa={fa.status_code} fr={fr.status_code}; fa_body={fa.text[:120]!r}; fr_body={fr.text[:120]!r}"
        if expect_status is not None and fa.status_code != expect_status:
            return False, f"expected {expect_status}, got fa={fa.status_code} fr={fr.status_code}"
        if compare == "status":
            return True, ""
        if compare == "text":
            if fa.text != fr.text:
                return False, f"text: fa={fa.text[:120]!r}; fr={fr.text[:120]!r}"
            return True, ""
        try:
            fj = fa.json()
        except Exception:
            fj = fa.text
        try:
            rj = fr.json()
        except Exception:
            rj = fr.text
        if compare == "keys":
            if isinstance(fj, dict) and isinstance(rj, dict):
                if set(fj.keys()) != set(rj.keys()):
                    return False, f"keys: fa={sorted(fj.keys())} fr={sorted(rj.keys())}"
                return True, ""
            if fj != rj:
                return False, f"body: fa={_t(fj)} fr={_t(rj)}"
            return True, ""
        # default: deep-json
        if fj != rj:
            return False, f"body: fa={_t(fj)}\n      fr={_t(rj)}"
        return True, ""


def _t(x, n=160):
    s = json.dumps(x, default=str, sort_keys=True) if not isinstance(x, str) else x
    return s if len(s) <= n else s[:n] + "..."


# ───────── DB verifier ───────────────────────────────────────────────

class _OneDBVerifier:
    def __init__(self, verify_url, suffix):
        import psycopg
        self.conn = psycopg.connect(verify_url, autocommit=True)
        self.suffix = suffix

    def table(self, base):
        return f"{base}_{self.suffix}"

    def count(self, base):
        cur = self.conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {self.table(base)}")
        n = int(cur.fetchone()[0])
        cur.close()
        return n

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


class DBVerifier:
    """Holds two psycopg connections, one for FA DB and one for FR DB."""
    def __init__(self, fa_url, fr_url, suffix):
        self.fa = _OneDBVerifier(fa_url, suffix)
        self.fr = _OneDBVerifier(fr_url, suffix)

    def count_fa(self, base):
        return self.fa.count(base)

    def count_fr(self, base):
        return self.fr.count(base)

    def close(self):
        self.fa.close()
        self.fr.close()


# ───────── test battery for a single driver ──────────────────────────

def run_battery_sync(h: Harness, v: DBVerifier, r: Results, async_mode=False):
    """Runs ~150 behavior tests vs both servers.

    async_mode=True tailors for the async app (which has fewer endpoints:
    no scoped session, no engine.begin, no merge/expire/expunge helpers).
    """
    cat = "crud.users"
    # --- USERS CRUD ----------------------------------------------------
    h.reset()

    h.check(cat, "create", "post", "/users", json={"email": "a@x", "name": "alice", "age": 30})
    h.check(cat, "create_dup", "post", "/users", json={"email": "a@x", "name": "alice2"}, expect_status=409)
    h.check(cat, "get_by_id_1", "get", "/users/1")
    h.check(cat, "get_by_id_404", "get", "/users/9999", expect_status=404)
    h.check(cat, "by_email", "get", "/users/by-email/a@x")
    h.check(cat, "by_email_404", "get", "/users/by-email/missing@x", expect_status=404)
    h.check(cat, "or_none_found", "get", "/users/1/or-none")
    h.check(cat, "or_none_missing", "get", "/users/9999/or-none")
    h.check(cat, "first", "get", "/q/users-first")
    h.check(cat, "list", "get", "/users")
    h.check(cat, "list_limit", "get", "/users?limit=1")
    h.check(cat, "list_offset", "get", "/users?limit=1&offset=0")
    h.check(cat, "put", "put", "/users/1", json={"email": "a@x", "name": "ALICE", "age": 31})
    # Create more users
    for i, e in enumerate(["b@x", "c@x", "d@x"]):
        h.check(cat, f"mk_{i}", "post", "/users", json={"email": e, "name": f"u{i}", "age": 20 + i})
    h.check(cat, "list_after", "get", "/users?order=id")
    h.check(cat, "del", "delete", "/users/4")
    h.check(cat, "list_after_del", "get", "/users")

    # DB-verify row counts match (both FA and FR DBs independently)
    try:
        fa_count = h.fa.get("/__count/users").json()["n"]
        fr_count = h.fr.get("/__count/users").json()["n"]
        db_fa = v.count_fa("users")
        db_fr = v.count_fr("users")
        r.record("db.verify", "user_count_fa", fa_count == db_fa, f"api_fa={fa_count} db_fa={db_fa}")
        r.record("db.verify", "user_count_fr", fr_count == db_fr, f"api_fr={fr_count} db_fr={db_fr}")
        r.record("db.verify", "user_count_same", fa_count == fr_count, f"fa={fa_count} fr={fr_count}")
    except Exception as e:
        r.record("db.verify", "user_count", False, str(e))

    # --- CATEGORIES ----------------------------------------------------
    cat = "crud.categories"
    h.check(cat, "create_1", "post", "/categories", json={"name": "catA"})
    h.check(cat, "create_2", "post", "/categories", json={"name": "catB"})
    h.check(cat, "create_dup", "post", "/categories", json={"name": "catA"}, expect_status=409)
    h.check(cat, "list", "get", "/categories")

    # --- ITEMS CRUD + relationships -----------------------------------
    cat = "crud.items"
    h.check(cat, "create_1", "post", "/items", json={"title": "i1", "price": 9.99, "quantity": 5, "owner_id": 1, "category_id": 1})
    h.check(cat, "create_2", "post", "/items", json={"title": "i2", "price": 19.99, "quantity": 1, "owner_id": 1, "category_id": 2})
    h.check(cat, "create_3", "post", "/items", json={"title": "i3", "price": 0.5, "quantity": 100, "owner_id": 2, "category_id": 1, "status": "active"})
    h.check(cat, "create_4", "post", "/items", json={"title": "i4", "price": 4.0, "quantity": 10, "owner_id": 2, "status": "archived"})
    h.check(cat, "create_5", "post", "/items", json={"title": "i5", "price": 50.0, "quantity": 2, "owner_id": 3, "status": "active"})
    h.check(cat, "get_1", "get", "/items/1")
    h.check(cat, "get_404", "get", "/items/9999", expect_status=404)
    h.check(cat, "by_owner_1", "get", "/q/items-by-owner/1")
    h.check(cat, "by_owner_2", "get", "/q/items-by-owner/2")
    h.check(cat, "by_owner_empty", "get", "/q/items-by-owner/9999")
    h.check(cat, "dup_owner_title", "post", "/items", json={"title": "i1", "price": 1.0, "owner_id": 1}, expect_status=409)
    h.check(cat, "bad_owner", "post", "/items", json={"title": "x", "price": 1.0, "owner_id": 999}, expect_status=400)
    h.check(cat, "selectin", "get", "/q/items-by-owner-selectin/1")
    h.check(cat, "selectin_404", "get", "/q/items-by-owner-selectin/9999", expect_status=404)
    if not async_mode:
        h.check(cat, "subqueryload", "get", "/q/items-by-owner-subqueryload/1")
    h.check(cat, "joinedload_user", "get", "/q/items-by-owner-joinedload/1")
    if not async_mode:
        h.check(cat, "contains_eager", "get", "/q/items-contains-eager/1")
    h.check(cat, "with_owner", "get", "/items/1/with-owner")

    # DB-verify items count on both
    try:
        fa_n = h.fa.get("/__count/items").json()["n"]
        fr_n = h.fr.get("/__count/items").json()["n"]
        db_fa = v.count_fa("items")
        db_fr = v.count_fr("items")
        r.record("db.verify", "items_count_fa", fa_n == db_fa, f"api_fa={fa_n} db_fa={db_fa}")
        r.record("db.verify", "items_count_fr", fr_n == db_fr, f"api_fr={fr_n} db_fr={db_fr}")
        r.record("db.verify", "items_count_same", fa_n == fr_n, f"fa={fa_n} fr={fr_n}")
    except Exception as e:
        r.record("db.verify", "items_count", False, str(e))

    # --- FILTER EXPRESSIONS -------------------------------------------
    cat = "filter"
    h.check(cat, "between", "get", "/q/items-filter?min_price=1&max_price=20")
    h.check(cat, "status_draft", "get", "/q/items-filter?status=draft")
    h.check(cat, "status_active", "get", "/q/items-filter?status=active")
    h.check(cat, "status_archived", "get", "/q/items-filter?status=archived")
    h.check(cat, "like", "get", "/q/items-filter?title_like=i")
    h.check(cat, "like_none", "get", "/q/items-filter?title_like=XX")
    h.check(cat, "ilike", "get", "/q/items-filter-ilike?pat=I")
    h.check(cat, "ilike_digits", "get", "/q/items-filter-ilike?pat=1")
    h.check(cat, "in_clause", "get", "/q/items-filter-in?ids=1,2,3")
    h.check(cat, "in_empty", "get", "/q/items-filter-in?ids=")
    h.check(cat, "in_missing", "get", "/q/items-filter-in?ids=999,1000")
    h.check(cat, "and_clause", "get", "/q/items-filter-and?min_q=5&max_p=100")
    h.check(cat, "or_clause", "get", "/q/items-filter-or?q=1&p=9.99")
    h.check(cat, "not_clause", "get", "/q/items-filter-not?status=draft")

    # --- AGGREGATES ---------------------------------------------------
    cat = "aggregate"
    h.check(cat, "stats", "get", "/q/items-stats")
    h.check(cat, "stats_by_status", "get", "/q/items-stats-by-status")
    h.check(cat, "having_1", "get", "/q/items-having?min_count=1")
    h.check(cat, "having_2", "get", "/q/items-having?min_count=2")
    h.check(cat, "having_99", "get", "/q/items-having?min_count=99")

    # --- JOINS --------------------------------------------------------
    cat = "join"
    h.check(cat, "inner", "get", "/q/items-join-owner")
    h.check(cat, "left", "get", "/q/items-left-join-category")

    # --- SUBQUERIES / CTES / WINDOW ------------------------------------
    cat = "subquery"
    h.check(cat, "sub_max", "get", "/q/items-subquery-max")
    h.check(cat, "cte_max", "get", "/q/items-cte-max")
    h.check(cat, "window_row_number", "get", "/q/items-window")

    # --- RAW SQL ------------------------------------------------------
    cat = "raw"
    h.check(cat, "count_users", "get", "/raw/count-users")
    h.check(cat, "user_name", "get", "/raw/user-name/1")
    h.check(cat, "user_name_missing", "get", "/raw/user-name/9999")

    # --- SET OPERATIONS -----------------------------------------------
    cat = "setops"
    h.check(cat, "union", "get", "/q/items-union")
    h.check(cat, "intersect", "get", "/q/items-intersect")

    # --- BULK ---------------------------------------------------------
    cat = "bulk"
    bulk_payload = [
        {"title": f"bulk_{i}", "price": float(i), "quantity": i, "owner_id": 1}
        for i in range(1, 4)
    ]
    h.check(cat, "add_all", "post", "/bulk/items", json=bulk_payload)
    h.check(cat, "insert_core", "post", "/bulk/insert-core",
            json=[{"title": f"core_{i}", "price": float(i), "owner_id": 2} for i in range(1, 3)])
    h.check(cat, "update_core", "post", "/bulk/update-core?owner_id=1&new_price=42.0")
    h.check(cat, "update_core_noop", "post", "/bulk/update-core?owner_id=9999&new_price=1.0")
    h.check(cat, "delete_core", "post", "/bulk/delete-core?owner_id=2")
    h.check(cat, "delete_core_noop", "post", "/bulk/delete-core?owner_id=9999")
    h.check(cat, "returning", "post", "/returning/insert-item", json={"title": "ret1", "price": 1.0, "owner_id": 3})

    # Verify the update actually happened in each DB
    for label, vdb in [("fa", v.fa), ("fr", v.fr)]:
        try:
            cur = vdb.conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM {vdb.table('items')} WHERE owner_id = 1 AND price = 42.0")
            n = int(cur.fetchone()[0])
            cur.close()
            r.record("db.verify", f"bulk_update_applied_{label}", n >= 1, f"rows: {n}")
        except Exception as e:
            r.record("db.verify", f"bulk_update_applied_{label}", False, str(e))

    # --- SESSION LIFECYCLE --------------------------------------------
    cat = "session"
    h.check(cat, "flush_no_commit", "post", "/session/flush-no-commit",
            json={"email": "flush@x", "name": "flush"})
    h.check(cat, "savepoint_ok", "post", "/session/savepoint",
            json={"email": "sp1@x", "name": "sp1"})
    h.check(cat, "savepoint_rollback", "post", "/session/savepoint-rollback",
            json={"email": "sprb@x", "name": "sprb"})
    h.check(cat, "refresh", "post", "/session/refresh/1")
    if not async_mode:
        h.check(cat, "expire", "post", "/session/expire/1")
        h.check(cat, "expunge", "post", "/session/expunge/1")
        h.check(cat, "merge", "post", "/session/merge/1?new_name=MERGED")
        h.check(cat, "scoped_session", "get", "/session/scoped")

    # --- ORDER LINES (composite PK) -----------------------------------
    cat = "composite_pk"
    h.check(cat, "create_1", "post", "/order-lines?order_id=1&line_no=1&sku=SKU1&qty=3")
    h.check(cat, "create_2", "post", "/order-lines?order_id=1&line_no=2&sku=SKU2&qty=5")
    h.check(cat, "dup", "post", "/order-lines?order_id=1&line_no=1&sku=SKUX", expect_status=409)
    h.check(cat, "get_1_1", "get", "/order-lines/1/1")
    h.check(cat, "get_1_2", "get", "/order-lines/1/2")
    h.check(cat, "get_missing", "get", "/order-lines/999/1", expect_status=404)

    # --- TAGS ARRAY / JSON --------------------------------------------
    cat = "arrays_json"
    h.check(cat, "tagarr_create", "post", "/tagarr?name=t1", json=["red", "green", "blue"])
    h.check(cat, "tagarr_create_empty", "post", "/tagarr?name=t2", json=[])
    h.check(cat, "tagarr_get", "get", "/tagarr/1")
    h.check(cat, "tags_json_item1", "post", "/items/1/tags-json",
            json={"colors": ["red", "green"], "rating": 5, "meta": {"deep": True}})
    h.check(cat, "tags_json_get_back", "get", "/items/1")

    # --- ERROR PROPAGATION --------------------------------------------
    cat = "errors"
    h.check(cat, "dup_email", "post", "/err/duplicate-email?email=dup1@x", expect_status=409)
    h.check(cat, "no_result", "get", "/err/no-result", expect_status=404)
    h.check(cat, "multi_result", "post", "/err/multi-result", expect_status=400)
    if not async_mode:
        h.check(cat, "pending_rollback", "post", "/err/pending-rollback?email=pend@x")

    # --- ENUM ---------------------------------------------------------
    cat = "enum"
    h.check(cat, "set_active", "post", "/items/1/status?status=active")
    h.check(cat, "set_archived", "post", "/items/1/status?status=archived")
    h.check(cat, "set_bad", "post", "/items/1/status?status=bogus", expect_status=422)

    # --- PAGINATION ---------------------------------------------------
    cat = "pagination"
    h.check(cat, "page_1_10", "get", "/q/items-page?page=1&size=10")
    h.check(cat, "page_2_2", "get", "/q/items-page?page=2&size=2")
    h.check(cat, "page_big", "get", "/q/items-page?page=999&size=10")

    # --- BACKGROUND TASKS ---------------------------------------------
    cat = "background"
    h.check(cat, "bg_queue", "post", "/bg/create-user?email=bg1@x&name=bgu&key=k1")
    # Wait for BG to complete
    time.sleep(0.5)
    h.check(cat, "bg_result", "get", "/bg/result/k1")

    # --- ENGINE.begin (sync only) -------------------------------------
    if not async_mode:
        cat = "engine"
        h.check(cat, "engine_begin", "post", "/engine/begin?email=eng@x&name=eng")

    # --- LATEST (response_model from_attributes) ----------------------
    if not async_mode:
        cat = "response_model"
        h.check(cat, "latest_item", "get", "/q/items-latest")

    # --- COUNT endpoints (always 200) ---------------------------------
    cat = "counts"
    h.check(cat, "users", "get", "/__count/users")
    h.check(cat, "items", "get", "/__count/items")
    h.check(cat, "categories", "get", "/__count/categories")

    # --- Extended CRUD variations -------------------------------------
    cat = "ext.users"
    # Extra users for broader coverage
    for i in range(10, 25):
        h.check(cat, f"create_u{i}", "post", "/users",
                json={"email": f"ext{i}@x", "name": f"User{i}", "age": 20 + i})
    for i in range(10, 20):
        h.check(cat, f"get_u{i}", "get", f"/users/by-email/ext{i}@x")

    cat = "ext.items"
    # Extra items varying price/quantity/status
    for i in range(1, 12):
        h.check(cat, f"create_i{i}", "post", "/items",
                json={"title": f"xt{i}", "price": float(i) * 1.5,
                      "quantity": i * 2, "owner_id": 1,
                      "status": ["draft", "active", "archived"][i % 3]})
    # Filter boundary tests
    h.check(cat, "filter_price_boundary_0", "get", "/q/items-filter?min_price=0&max_price=0")
    h.check(cat, "filter_price_large", "get", "/q/items-filter?min_price=0&max_price=1000000")
    h.check(cat, "filter_title_empty", "get", "/q/items-filter?title_like=")
    h.check(cat, "filter_negative", "get", "/q/items-filter?min_price=-100")

    cat = "ext.sql"
    # Raw SQL edge cases
    h.check(cat, "sum_all_prices", "get", "/q/items-stats")
    h.check(cat, "count_by_status_draft", "get", "/q/items-filter?status=draft")
    h.check(cat, "count_by_status_active", "get", "/q/items-filter?status=active")
    h.check(cat, "filter_in_all", "get", "/q/items-filter-in?ids=1,2,3,4,5")
    h.check(cat, "filter_in_partial", "get", "/q/items-filter-in?ids=1,999,2,9999")

    cat = "ext.pagination"
    for size in (1, 2, 5, 20, 100):
        h.check(cat, f"size_{size}", "get", f"/q/items-page?size={size}&page=1")
    for page in (1, 2, 3):
        h.check(cat, f"page_{page}", "get", f"/q/items-page?size=3&page={page}")

    cat = "ext.joins"
    h.check(cat, "join_owner_after_bulk", "get", "/q/items-join-owner")
    h.check(cat, "left_cat_after_bulk", "get", "/q/items-left-join-category")
    h.check(cat, "union_after_bulk", "get", "/q/items-union")
    h.check(cat, "intersect_after_bulk", "get", "/q/items-intersect")
    h.check(cat, "cte_after_bulk", "get", "/q/items-cte-max")
    h.check(cat, "window_after_bulk", "get", "/q/items-window")
    h.check(cat, "subquery_after_bulk", "get", "/q/items-subquery-max")

    cat = "ext.errors"
    h.check(cat, "get_neg", "get", "/items/-1", expect_status=404)
    h.check(cat, "get_0", "get", "/items/0", expect_status=404)
    h.check(cat, "get_str", "get", "/items/not-a-number", expect_status=422)
    h.check(cat, "get_user_neg", "get", "/users/-1", expect_status=404)
    h.check(cat, "get_user_str", "get", "/users/not-a-number", expect_status=422)

    cat = "ext.ordering"
    h.check(cat, "order_by_id", "get", "/users?order=id")
    h.check(cat, "order_by_name", "get", "/users?order=name")
    h.check(cat, "order_by_email", "get", "/users?order=email")
    h.check(cat, "order_by_age", "get", "/users?order=age")
    # invalid order falls back to id
    h.check(cat, "order_by_bogus", "get", "/users?order=bogus")

    cat = "ext.limit_offset"
    h.check(cat, "lim_5", "get", "/users?limit=5")
    h.check(cat, "lim_100", "get", "/users?limit=100")
    h.check(cat, "lim_0", "get", "/users?limit=0")
    h.check(cat, "off_5", "get", "/users?offset=5")
    h.check(cat, "off_big", "get", "/users?offset=9999")
    h.check(cat, "lim_off", "get", "/users?limit=3&offset=2")

    cat = "ext.relationships"
    # repeated selectin / joinedload / subqueryload on existing data
    h.check(cat, "selectin_1", "get", "/q/items-by-owner-selectin/1")
    h.check(cat, "joinedload_1", "get", "/q/items-by-owner-joinedload/1")
    if not async_mode:
        h.check(cat, "subq_1", "get", "/q/items-by-owner-subqueryload/1")
        h.check(cat, "ce_1", "get", "/q/items-contains-eager/1")

    # --- Final DB cross-check -----------------------------------------
    cat = "db.final"
    try:
        fa_u = h.fa.get("/__count/users").json()["n"]
        fr_u = h.fr.get("/__count/users").json()["n"]
        db_fa = v.count_fa("users")
        db_fr = v.count_fr("users")
        r.record(cat, "users_api_db_fa", fa_u == db_fa, f"api_fa={fa_u} db_fa={db_fa}")
        r.record(cat, "users_api_db_fr", fr_u == db_fr, f"api_fr={fr_u} db_fr={db_fr}")
        r.record(cat, "users_fa_fr", fa_u == fr_u, f"fa={fa_u} fr={fr_u}")
    except Exception as e:
        r.record(cat, "users_3_way", False, str(e))

    try:
        fa_i = h.fa.get("/__count/items").json()["n"]
        fr_i = h.fr.get("/__count/items").json()["n"]
        db_fa = v.count_fa("items")
        db_fr = v.count_fr("items")
        r.record(cat, "items_api_db_fa", fa_i == db_fa, f"api_fa={fa_i} db_fa={db_fa}")
        r.record(cat, "items_api_db_fr", fr_i == db_fr, f"api_fr={fr_i} db_fr={db_fr}")
        r.record(cat, "items_fa_fr", fa_i == fr_i, f"fa={fa_i} fr={fr_i}")
    except Exception as e:
        r.record(cat, "items_3_way", False, str(e))


# ───────── driver orchestration ──────────────────────────────────────

def run_driver(driver: str, verbose=False):
    fa_port = FASTAPI_PORTS[driver]
    fr_port = FASTAPI_RS_PORTS[driver]
    print(f"\n{BOLD}{CYAN}=== Driver: {driver} (FA={fa_port}, FR={fr_port}) ==={RESET}")

    fa_proc = start_uvicorn(driver, fa_port)
    fr_proc = start_fastapi_rs(driver, fr_port)

    try:
        fa_ok = wait_for_health(fa_port)
        fr_ok = wait_for_health(fr_port)
        if not fa_ok:
            print(f"{RED}FastAPI/uvicorn failed to start on {fa_port}{RESET}")
            try:
                with open(fa_proc._err_path, "rb") as f:
                    print(f"stderr: {f.read().decode()[-800:]}")
            except Exception:
                pass
            return None
        if not fr_ok:
            print(f"{RED}fastapi-rs failed to start on {fr_port}{RESET}")
            try:
                with open(fr_proc._err_path, "rb") as f:
                    print(f"stderr: {f.read().decode()[-800:]}")
            except Exception:
                pass
            return None
        print(f"  both servers up")

        async_mode = (driver == "async")
        h = Harness(fa_port, fr_port, driver, verbose=verbose)
        v = DBVerifier(FA_VERIFY_URL[driver], FR_VERIFY_URL[driver], driver)
        try:
            run_battery_sync(h, v, h.results, async_mode=async_mode)
        finally:
            h.close()
            v.close()
        return h.results

    finally:
        for p in (fa_proc, fr_proc):
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass


# ───────── report ────────────────────────────────────────────────────

def print_report(results_by_driver):
    print(f"\n{BOLD}=== PARITY REPORT ==={RESET}")
    grand_total = grand_pass = 0
    for driver, r in results_by_driver.items():
        if r is None:
            print(f"\n{BOLD}Driver {driver}: SKIPPED (server failed to start){RESET}")
            continue
        grand_total += r.total
        grand_pass += r.passed
        pct = (r.passed * 100.0 / r.total) if r.total else 0.0
        color = GREEN if r.failed == 0 else (YELLOW if pct > 80 else RED)
        print(f"\n{BOLD}Driver {driver}: {color}{r.passed}/{r.total} ({pct:.1f}%){RESET}")
        # per-category
        for c, (ok, n) in sorted(r.by_category.items()):
            mark = GREEN if ok == n else RED
            print(f"  {mark}[{ok}/{n}]{RESET} {c}")
        if r.failures:
            print(f"\n  {BOLD}Top failures ({min(15, len(r.failures))}/{len(r.failures)}):{RESET}")
            for cat, tid, det in r.failures[:15]:
                print(f"    - [{cat}] {tid}: {det[:170]}")

    pct = (grand_pass * 100.0 / grand_total) if grand_total else 0.0
    color = GREEN if grand_total == grand_pass else (YELLOW if pct > 80 else RED)
    print(f"\n{BOLD}GRAND TOTAL: {color}{grand_pass}/{grand_total} ({pct:.1f}%){RESET}")


# ───────── main ──────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--driver", choices=["pg3", "async", "pg2", "all"], default="all")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if not ensure_postgres():
        print(f"{RED}Cannot reach postgres on localhost:5432 -- aborting.{RESET}")
        sys.exit(2)

    drivers = ["pg3", "async", "pg2"] if args.driver == "all" else [args.driver]
    out = {}
    for d in drivers:
        out[d] = run_driver(d, verbose=args.verbose)

    print_report(out)

    any_failed = any(r and r.failed > 0 for r in out.values())
    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    main()
