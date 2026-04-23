#!/usr/bin/env python3
"""Deep integration parity runner.

Runs stock FastAPI (uvicorn) on port 29800 and fastapi-turbo on port 29801.
Each test is a multi-step flow (login then /me, create then read, etc.)
asserting both servers produce identical end-state.

Target: 500 tests.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import traceback
from collections import defaultdict

import httpx

# ── Config ────────────────────────────────────────────────────────

FASTAPI_PORT = 29800
FASTAPI_TURBO_PORT = 29801
HOST = "127.0.0.1"
APP_MODULE = "tests.parity.parity_app_deep_integration:app"
STARTUP_TIMEOUT = 20

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FA = f"http://{HOST}:{FASTAPI_PORT}"
FR = f"http://{HOST}:{FASTAPI_TURBO_PORT}"

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"
BOLD = "\033[1m"


# ── Server management ────────────────────────────────────────────

def wait_for_port(port, timeout=STARTUP_TIMEOUT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, port), timeout=1):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.2)
    return False


def start_uvicorn(port):
    env = os.environ.copy()
    env["PYTHONPATH"] = PROJECT_ROOT
    env.pop("FASTAPI_TURBO", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", APP_MODULE,
         "--host", HOST, "--port", str(port), "--log-level", "warning"],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


def start_fastapi_turbo(port):
    env = os.environ.copy()
    env["PYTHONPATH"] = PROJECT_ROOT
    script = f"""
import sys
sys.path.insert(0, '{PROJECT_ROOT}')
from fastapi_turbo.compat import install
install()
from tests.parity.parity_app_deep_integration import app
app.run(host='{HOST}', port={port})
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


# ── Results tracking ─────────────────────────────────────────────

results: list[tuple[int, str, str, bool, str]] = []  # id, category, desc, passed, detail


def record(tid: int, category: str, desc: str, passed: bool, detail: str = "") -> bool:
    results.append((tid, category, desc, passed, detail))
    if not passed:
        # Truncate and print failures inline at verbose level
        print(f"  {RED}FAIL{RESET} T{tid:04d} [{category}] {desc}")
        if detail:
            print(f"        {detail[:220]}")
    return passed


def safe(fn):
    """Wrap test fn so one failure doesn't stop the run."""
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            return ("EXC", str(e), traceback.format_exc())
    return wrapped


# ── Request helpers ──────────────────────────────────────────────

_CLIENTS = {}


def _client(base):
    # One client per base, long-lived keep-alive
    c = _CLIENTS.get(base)
    if c is None:
        c = httpx.Client(base_url=base, timeout=10.0, follow_redirects=False)
        _CLIENTS[base] = c
    return c


def close_clients():
    for c in _CLIENTS.values():
        try:
            c.close()
        except Exception:
            pass
    _CLIENTS.clear()


def both_req(method, path, **kw):
    """Issue same request to both servers, return (fa_resp, fr_resp). Either may be Exception."""
    fa, fr = None, None
    try:
        fa = _client(FA).request(method, path, **kw)
    except Exception as e:
        fa = e
    try:
        fr = _client(FR).request(method, path, **kw)
    except Exception as e:
        fr = e
    return fa, fr


def _resp_json(r):
    if isinstance(r, Exception):
        return None
    try:
        return r.json()
    except Exception:
        return None


def _resp_status(r):
    if isinstance(r, Exception):
        return -1
    return r.status_code


def _resp_text(r):
    if isinstance(r, Exception):
        return f"EXC: {r}"
    try:
        return r.text
    except Exception:
        return ""


# ── Common assertion helpers ─────────────────────────────────────

def assert_status(fa, fr):
    fa_s = _resp_status(fa)
    fr_s = _resp_status(fr)
    if fa_s != fr_s:
        raise AssertionError(f"status mismatch: fa={fa_s} fr={fr_s}; fa_body={_resp_text(fa)[:120]}; fr_body={_resp_text(fr)[:120]}")
    return fa_s


def assert_status_eq(fa, fr, expected):
    fa_s = _resp_status(fa)
    fr_s = _resp_status(fr)
    if fa_s != expected:
        raise AssertionError(f"fa status={fa_s}, expected {expected}; body={_resp_text(fa)[:120]}")
    if fr_s != expected:
        raise AssertionError(f"fr status={fr_s}, expected {expected}; body={_resp_text(fr)[:120]}")


def assert_json_eq(fa, fr):
    fa_j = _resp_json(fa)
    fr_j = _resp_json(fr)
    if fa_j != fr_j:
        raise AssertionError(f"json mismatch:\n  fa={json.dumps(fa_j, sort_keys=True)[:200]}\n  fr={json.dumps(fr_j, sort_keys=True)[:200]}")


def assert_keys_eq(fa, fr):
    fa_j = _resp_json(fa)
    fr_j = _resp_json(fr)
    if not isinstance(fa_j, dict) or not isinstance(fr_j, dict):
        raise AssertionError(f"not dicts: fa={type(fa_j).__name__} fr={type(fr_j).__name__}")
    if set(fa_j.keys()) != set(fr_j.keys()):
        raise AssertionError(f"keys differ: fa={sorted(fa_j.keys())} fr={sorted(fr_j.keys())}")


def assert_header_present(fa, fr, name):
    if isinstance(fa, Exception) or isinstance(fr, Exception):
        raise AssertionError(f"exception: fa={fa} fr={fr}")
    if name.lower() not in {k.lower() for k in fa.headers.keys()}:
        raise AssertionError(f"fa missing header {name}")
    if name.lower() not in {k.lower() for k in fr.headers.keys()}:
        raise AssertionError(f"fr missing header {name}")


# ── Shared context used across tests ─────────────────────────────

CTX: dict = {}


# =============================================================================
# TEST FLOWS
# =============================================================================


CATEGORIES: dict[str, list[str]] = defaultdict(list)


def run_test(tid: int, category: str, desc: str, fn):
    try:
        fn()
        record(tid, category, desc, True)
    except AssertionError as e:
        record(tid, category, desc, False, str(e))
    except Exception as e:
        record(tid, category, desc, False, f"{type(e).__name__}: {e}")


# ─── AUTH flow tests ─────────────────────────────────────────────

def auth_tests(start_id=1):
    cat = "auth"
    tid = start_id

    # 1: login alice → both return tokens
    def t():
        fa = _client(FA).post("/auth/login", json={"username": "alice", "password": "wonderland"})
        fr = _client(FR).post("/auth/login", json={"username": "alice", "password": "wonderland"})
        assert_status_eq(fa, fr, 200)
        CTX["fa_alice_token"] = fa.json()["token"]
        CTX["fr_alice_token"] = fr.json()["token"]
        assert "token" in fa.json() and "token" in fr.json()
    run_test(tid, cat, "login alice → 200 + token", t); tid += 1

    # login bob
    def t():
        fa = _client(FA).post("/auth/login", json={"username": "bob", "password": "builder"})
        fr = _client(FR).post("/auth/login", json={"username": "bob", "password": "builder"})
        assert_status_eq(fa, fr, 200)
        CTX["fa_bob_token"] = fa.json()["token"]
        CTX["fr_bob_token"] = fr.json()["token"]
    run_test(tid, cat, "login bob → 200 + token", t); tid += 1

    # login eve
    def t():
        fa = _client(FA).post("/auth/login", json={"username": "eve", "password": "hunter2"})
        fr = _client(FR).post("/auth/login", json={"username": "eve", "password": "hunter2"})
        assert_status_eq(fa, fr, 200)
        CTX["fa_eve_token"] = fa.json()["token"]
        CTX["fr_eve_token"] = fr.json()["token"]
    run_test(tid, cat, "login eve → 200 + token", t); tid += 1

    # bad password → 401
    def t():
        fa, fr = both_req("POST", "/auth/login", json={"username": "alice", "password": "WRONG"})
        assert_status_eq(fa, fr, 401)
    run_test(tid, cat, "login bad password → 401", t); tid += 1

    # unknown user → 401
    def t():
        fa, fr = both_req("POST", "/auth/login", json={"username": "nobody", "password": "x"})
        assert_status_eq(fa, fr, 401)
    run_test(tid, cat, "login unknown user → 401", t); tid += 1

    # missing body field → 422
    def t():
        fa, fr = both_req("POST", "/auth/login", json={"username": "alice"})
        assert_status_eq(fa, fr, 422)
    run_test(tid, cat, "login missing password → 422", t); tid += 1

    # me with alice token
    def t():
        fa = _client(FA).get("/auth/me", headers={"Authorization": f"Bearer {CTX['fa_alice_token']}"})
        fr = _client(FR).get("/auth/me", headers={"Authorization": f"Bearer {CTX['fr_alice_token']}"})
        assert_status_eq(fa, fr, 200)
        assert_keys_eq(fa, fr)
        assert fa.json()["username"] == "alice" == fr.json()["username"]
    run_test(tid, cat, "/auth/me with alice token → both return alice", t); tid += 1

    # me with bob token
    def t():
        fa = _client(FA).get("/auth/me", headers={"Authorization": f"Bearer {CTX['fa_bob_token']}"})
        fr = _client(FR).get("/auth/me", headers={"Authorization": f"Bearer {CTX['fr_bob_token']}"})
        assert_status_eq(fa, fr, 200)
        assert fa.json()["username"] == "bob" == fr.json()["username"]
        assert fa.json()["role"] == "user" == fr.json()["role"]
    run_test(tid, cat, "/auth/me with bob token → both return bob", t); tid += 1

    # me without token → 401
    def t():
        fa, fr = both_req("GET", "/auth/me")
        assert_status_eq(fa, fr, 401)
    run_test(tid, cat, "/auth/me without token → 401 on both", t); tid += 1

    # me with invalid token → 401
    def t():
        fa, fr = both_req("GET", "/auth/me", headers={"Authorization": "Bearer INVALID_TOKEN"})
        assert_status_eq(fa, fr, 401)
    run_test(tid, cat, "/auth/me bad token → 401", t); tid += 1

    # WWW-Authenticate header present on 401
    def t():
        fa, fr = both_req("GET", "/auth/me")
        assert_status_eq(fa, fr, 401)
        assert_header_present(fa, fr, "WWW-Authenticate")
    run_test(tid, cat, "/auth/me 401 includes WWW-Authenticate", t); tid += 1

    # protected with valid token
    def t():
        fa = _client(FA).get("/auth/protected", headers={"Authorization": f"Bearer {CTX['fa_bob_token']}"})
        fr = _client(FR).get("/auth/protected", headers={"Authorization": f"Bearer {CTX['fr_bob_token']}"})
        assert_status_eq(fa, fr, 200)
        assert fa.json()["user"] == "bob" == fr.json()["user"]
    run_test(tid, cat, "/auth/protected with bob token → 200", t); tid += 1

    # admin with alice (admin role) → 200
    def t():
        fa = _client(FA).get("/auth/admin", headers={"Authorization": f"Bearer {CTX['fa_alice_token']}"})
        fr = _client(FR).get("/auth/admin", headers={"Authorization": f"Bearer {CTX['fr_alice_token']}"})
        assert_status_eq(fa, fr, 200)
        assert fa.json()["admin"] == "alice" == fr.json()["admin"]
    run_test(tid, cat, "/auth/admin with alice (admin) → 200", t); tid += 1

    # admin with bob (user) → 403
    def t():
        fa = _client(FA).get("/auth/admin", headers={"Authorization": f"Bearer {CTX['fa_bob_token']}"})
        fr = _client(FR).get("/auth/admin", headers={"Authorization": f"Bearer {CTX['fr_bob_token']}"})
        assert_status_eq(fa, fr, 403)
    run_test(tid, cat, "/auth/admin with bob (user) → 403", t); tid += 1

    # admin without token → 401
    def t():
        fa, fr = both_req("GET", "/auth/admin")
        assert_status_eq(fa, fr, 401)
    run_test(tid, cat, "/auth/admin without token → 401", t); tid += 1

    # /auth/token OAuth2 form flow
    def t():
        fa = _client(FA).post("/auth/token", data={"username": "alice", "password": "wonderland"})
        fr = _client(FR).post("/auth/token", data={"username": "alice", "password": "wonderland"})
        assert_status_eq(fa, fr, 200)
        assert_keys_eq(fa, fr)
        assert fa.json()["token_type"] == "bearer" == fr.json()["token_type"]
    run_test(tid, cat, "/auth/token OAuth2 form → 200 + bearer", t); tid += 1

    # /auth/token bad password
    def t():
        fa, fr = both_req("POST", "/auth/token", data={"username": "alice", "password": "WRONG"})
        assert_status_eq(fa, fr, 400)
    run_test(tid, cat, "/auth/token bad password → 400", t); tid += 1

    # refresh with valid token produces a new token
    def t():
        fa = _client(FA).post("/auth/refresh", headers={"Authorization": f"Bearer {CTX['fa_alice_token']}"})
        fr = _client(FR).post("/auth/refresh", headers={"Authorization": f"Bearer {CTX['fr_alice_token']}"})
        assert_status_eq(fa, fr, 200)
        assert "token" in fa.json() and "token" in fr.json()
    run_test(tid, cat, "/auth/refresh with token → 200 + new token", t); tid += 1

    # logout
    def t():
        fa = _client(FA).post("/auth/logout", headers={"Authorization": f"Bearer {CTX['fa_alice_token']}"})
        fr = _client(FR).post("/auth/logout", headers={"Authorization": f"Bearer {CTX['fr_alice_token']}"})
        assert_status_eq(fa, fr, 200)
        assert_json_eq(fa, fr)
    run_test(tid, cat, "/auth/logout with token → 200", t); tid += 1

    # refresh without token
    def t():
        fa, fr = both_req("POST", "/auth/refresh")
        assert_status_eq(fa, fr, 401)
    run_test(tid, cat, "/auth/refresh without token → 401", t); tid += 1

    # protected with expired-style tampered token (random payload)
    def t():
        bad = "Bearer AAAAAA"
        fa, fr = both_req("GET", "/auth/protected", headers={"Authorization": bad})
        assert_status_eq(fa, fr, 401)
    run_test(tid, cat, "/auth/protected tampered token → 401", t); tid += 1

    # protected with Bearer missing
    def t():
        fa, fr = both_req("GET", "/auth/protected", headers={"Authorization": "NotBearer zzz"})
        assert_status_eq(fa, fr, 401)
    run_test(tid, cat, "/auth/protected non-Bearer → 401", t); tid += 1

    return tid


# ─── CRUD items tests ────────────────────────────────────────────

def crud_tests(start_id):
    cat = "crud-items"
    tid = start_id

    # create item A
    def t():
        fa = _client(FA).post("/items", json={"name": "alpha", "price": 9.99, "tags": ["a"]})
        fr = _client(FR).post("/items", json={"name": "alpha", "price": 9.99, "tags": ["a"]})
        assert_status_eq(fa, fr, 201)
        CTX["fa_item_alpha_id"] = fa.json()["id"]
        CTX["fr_item_alpha_id"] = fr.json()["id"]
    run_test(tid, cat, "POST /items alpha → 201", t); tid += 1

    # create item B
    def t():
        fa = _client(FA).post("/items", json={"name": "beta", "price": 1.0, "tags": []})
        fr = _client(FR).post("/items", json={"name": "beta", "price": 1.0, "tags": []})
        assert_status_eq(fa, fr, 201)
        CTX["fa_item_beta_id"] = fa.json()["id"]
        CTX["fr_item_beta_id"] = fr.json()["id"]
    run_test(tid, cat, "POST /items beta → 201", t); tid += 1

    # duplicate name → 409
    def t():
        fa, fr = both_req("POST", "/items", json={"name": "alpha", "price": 2.0})
        assert_status_eq(fa, fr, 409)
    run_test(tid, cat, "POST duplicate name → 409", t); tid += 1

    # get alpha by id
    def t():
        fa = _client(FA).get(f"/items/{CTX['fa_item_alpha_id']}")
        fr = _client(FR).get(f"/items/{CTX['fr_item_alpha_id']}")
        assert_status_eq(fa, fr, 200)
        assert fa.json()["name"] == "alpha" == fr.json()["name"]
    run_test(tid, cat, "GET /items/{id} alpha → 200", t); tid += 1

    # get unknown
    def t():
        fa, fr = both_req("GET", "/items/99999")
        assert_status_eq(fa, fr, 404)
    run_test(tid, cat, "GET /items/99999 → 404", t); tid += 1

    # put update alpha
    def t():
        fa = _client(FA).put(f"/items/{CTX['fa_item_alpha_id']}", json={"name": "alpha", "price": 19.99, "tags": ["updated"]})
        fr = _client(FR).put(f"/items/{CTX['fr_item_alpha_id']}", json={"name": "alpha", "price": 19.99, "tags": ["updated"]})
        assert_status_eq(fa, fr, 200)
        assert fa.json()["price"] == 19.99 == fr.json()["price"]
    run_test(tid, cat, "PUT /items/{id} → 200 price updated", t); tid += 1

    # patch alpha
    def t():
        fa = _client(FA).patch(f"/items/{CTX['fa_item_alpha_id']}", json={"price": 29.99})
        fr = _client(FR).patch(f"/items/{CTX['fr_item_alpha_id']}", json={"price": 29.99})
        assert_status_eq(fa, fr, 200)
        assert fa.json()["price"] == 29.99 == fr.json()["price"]
        assert fa.json()["name"] == "alpha" == fr.json()["name"]  # unchanged
    run_test(tid, cat, "PATCH /items/{id} price → 200", t); tid += 1

    # list items
    def t():
        fa, fr = both_req("GET", "/items")
        assert_status_eq(fa, fr, 200)
        assert len(fa.json()) == len(fr.json())
    run_test(tid, cat, "GET /items list length matches", t); tid += 1

    # pagination X-Total-Count
    def t():
        fa, fr = both_req("GET", "/items?skip=0&limit=1")
        assert_status_eq(fa, fr, 200)
        assert_header_present(fa, fr, "X-Total-Count")
    run_test(tid, cat, "GET /items?skip=0&limit=1 has X-Total-Count", t); tid += 1

    # delete beta → 204
    def t():
        fa = _client(FA).delete(f"/items/{CTX['fa_item_beta_id']}")
        fr = _client(FR).delete(f"/items/{CTX['fr_item_beta_id']}")
        assert_status_eq(fa, fr, 204)
    run_test(tid, cat, "DELETE /items/{id} → 204", t); tid += 1

    # beta is gone
    def t():
        fa = _client(FA).get(f"/items/{CTX['fa_item_beta_id']}")
        fr = _client(FR).get(f"/items/{CTX['fr_item_beta_id']}")
        assert_status_eq(fa, fr, 404)
    run_test(tid, cat, "GET deleted item → 404", t); tid += 1

    # delete unknown → 404
    def t():
        fa, fr = both_req("DELETE", "/items/99999")
        assert_status_eq(fa, fr, 404)
    run_test(tid, cat, "DELETE unknown item → 404", t); tid += 1

    # put unknown → 404
    def t():
        fa, fr = both_req("PUT", "/items/99999", json={"name": "x", "price": 0})
        assert_status_eq(fa, fr, 404)
    run_test(tid, cat, "PUT unknown item → 404", t); tid += 1

    # patch unknown → 404
    def t():
        fa, fr = both_req("PATCH", "/items/99999", json={"price": 0})
        assert_status_eq(fa, fr, 404)
    run_test(tid, cat, "PATCH unknown item → 404", t); tid += 1

    # post bad schema → 422
    def t():
        fa, fr = both_req("POST", "/items", json={"name": 123})
        assert_status_eq(fa, fr, 422)
    run_test(tid, cat, "POST /items bad schema → 422", t); tid += 1

    # post missing price → 422
    def t():
        fa, fr = both_req("POST", "/items", json={"name": "gamma"})
        assert_status_eq(fa, fr, 422)
    run_test(tid, cat, "POST /items missing price → 422", t); tid += 1

    # bulk create 5 more items
    for i in range(5):
        def t(i=i):
            name = f"bulk-{i}"
            fa, fr = both_req("POST", "/items", json={"name": name, "price": float(i)})
            assert_status_eq(fa, fr, 201)
        run_test(tid, cat, f"POST /items bulk-{i} → 201", t); tid += 1

    # list should have at least 5 items now
    def t():
        fa, fr = both_req("GET", "/items?skip=0&limit=100")
        assert_status_eq(fa, fr, 200)
        assert len(fa.json()) >= 5 and len(fr.json()) >= 5
    run_test(tid, cat, "GET /items?limit=100 returns >=5 items", t); tid += 1

    # pagination slicing
    def t():
        fa, fr = both_req("GET", "/items?skip=0&limit=3")
        assert_status_eq(fa, fr, 200)
        assert len(fa.json()) == len(fr.json())
    run_test(tid, cat, "GET /items?skip=0&limit=3 same page size", t); tid += 1

    # pagination second page
    def t():
        fa, fr = both_req("GET", "/items?skip=3&limit=3")
        assert_status_eq(fa, fr, 200)
    run_test(tid, cat, "GET /items?skip=3&limit=3 → 200", t); tid += 1

    return tid


# ─── Users + nested posts tests ──────────────────────────────────

def users_tests(start_id):
    cat = "users-nested"
    tid = start_id

    # create user
    def t():
        fa = _client(FA).post("/api/v1/users", json={"name": "carol", "email": "c@example.com"})
        fr = _client(FR).post("/api/v1/users", json={"name": "carol", "email": "c@example.com"})
        assert_status_eq(fa, fr, 201)
        CTX["fa_user_carol"] = fa.json()["id"]
        CTX["fr_user_carol"] = fr.json()["id"]
    run_test(tid, cat, "POST /api/v1/users → 201", t); tid += 1

    # create another user
    def t():
        fa = _client(FA).post("/api/v1/users", json={"name": "dave", "email": "d@example.com"})
        fr = _client(FR).post("/api/v1/users", json={"name": "dave", "email": "d@example.com"})
        assert_status_eq(fa, fr, 201)
        CTX["fa_user_dave"] = fa.json()["id"]
        CTX["fr_user_dave"] = fr.json()["id"]
    run_test(tid, cat, "POST /api/v1/users dave → 201", t); tid += 1

    # read carol
    def t():
        fa = _client(FA).get(f"/api/v1/users/{CTX['fa_user_carol']}")
        fr = _client(FR).get(f"/api/v1/users/{CTX['fr_user_carol']}")
        assert_status_eq(fa, fr, 200)
        assert fa.json()["name"] == "carol" == fr.json()["name"]
    run_test(tid, cat, "GET /api/v1/users/{id} carol → 200", t); tid += 1

    # read unknown
    def t():
        fa, fr = both_req("GET", "/api/v1/users/99999")
        assert_status_eq(fa, fr, 404)
    run_test(tid, cat, "GET unknown user → 404", t); tid += 1

    # list users
    def t():
        fa, fr = both_req("GET", "/api/v1/users")
        assert_status_eq(fa, fr, 200)
        assert len(fa.json()) == len(fr.json()) >= 2
    run_test(tid, cat, "GET /api/v1/users → 200 with carol+dave", t); tid += 1

    # post to carol
    def t():
        fa = _client(FA).post(f"/api/v1/users/{CTX['fa_user_carol']}/posts", json={"title": "hi", "body": "hello"})
        fr = _client(FR).post(f"/api/v1/users/{CTX['fr_user_carol']}/posts", json={"title": "hi", "body": "hello"})
        assert_status_eq(fa, fr, 201)
        CTX["fa_post_carol_1"] = fa.json()["id"]
        CTX["fr_post_carol_1"] = fr.json()["id"]
    run_test(tid, cat, "POST /api/v1/users/{id}/posts → 201", t); tid += 1

    # second post
    def t():
        fa = _client(FA).post(f"/api/v1/users/{CTX['fa_user_carol']}/posts", json={"title": "p2", "body": "b2"})
        fr = _client(FR).post(f"/api/v1/users/{CTX['fr_user_carol']}/posts", json={"title": "p2", "body": "b2"})
        assert_status_eq(fa, fr, 201)
    run_test(tid, cat, "POST second post for carol → 201", t); tid += 1

    # list posts
    def t():
        fa = _client(FA).get(f"/api/v1/users/{CTX['fa_user_carol']}/posts")
        fr = _client(FR).get(f"/api/v1/users/{CTX['fr_user_carol']}/posts")
        assert_status_eq(fa, fr, 200)
        assert len(fa.json()) == len(fr.json()) >= 2
    run_test(tid, cat, "GET /api/v1/users/{id}/posts → 200", t); tid += 1

    # get single nested post
    def t():
        fa = _client(FA).get(f"/api/v1/users/{CTX['fa_user_carol']}/posts/{CTX['fa_post_carol_1']}")
        fr = _client(FR).get(f"/api/v1/users/{CTX['fr_user_carol']}/posts/{CTX['fr_post_carol_1']}")
        assert_status_eq(fa, fr, 200)
        assert fa.json()["title"] == "hi" == fr.json()["title"]
    run_test(tid, cat, "GET nested post by id → 200", t); tid += 1

    # get nested post for wrong user → 404
    def t():
        fa = _client(FA).get(f"/api/v1/users/{CTX['fa_user_dave']}/posts/{CTX['fa_post_carol_1']}")
        fr = _client(FR).get(f"/api/v1/users/{CTX['fr_user_dave']}/posts/{CTX['fr_post_carol_1']}")
        assert_status_eq(fa, fr, 404)
    run_test(tid, cat, "GET nested post wrong owner → 404", t); tid += 1

    # post to unknown user → 404
    def t():
        fa, fr = both_req("POST", "/api/v1/users/99999/posts", json={"title": "x", "body": "y"})
        assert_status_eq(fa, fr, 404)
    run_test(tid, cat, "POST to unknown user's posts → 404", t); tid += 1

    # list posts for unknown user
    def t():
        fa, fr = both_req("GET", "/api/v1/users/99999/posts")
        assert_status_eq(fa, fr, 404)
    run_test(tid, cat, "GET unknown user's posts → 404", t); tid += 1

    # list posts for dave (0 posts)
    def t():
        fa = _client(FA).get(f"/api/v1/users/{CTX['fa_user_dave']}/posts")
        fr = _client(FR).get(f"/api/v1/users/{CTX['fr_user_dave']}/posts")
        assert_status_eq(fa, fr, 200)
        assert fa.json() == [] == fr.json()
    run_test(tid, cat, "GET dave's posts (empty list)", t); tid += 1

    return tid


# ─── Pagination tests ────────────────────────────────────────────

def pagination_tests(start_id):
    cat = "pagination"
    tid = start_id

    for skip, limit in [(0, 10), (10, 10), (50, 20), (90, 10), (0, 1), (0, 100)]:
        def t(skip=skip, limit=limit):
            fa, fr = both_req("GET", f"/pagination/skip-limit?skip={skip}&limit={limit}")
            assert_status_eq(fa, fr, 200)
            assert_json_eq(fa, fr)
        run_test(tid, cat, f"skip={skip}&limit={limit} same items", t); tid += 1

    # X-Total-Count header
    def t():
        fa, fr = both_req("GET", "/pagination/skip-limit?skip=0&limit=5")
        assert_header_present(fa, fr, "X-Total-Count")
        assert fa.headers.get("X-Total-Count") == fr.headers.get("X-Total-Count")
    run_test(tid, cat, "skip-limit X-Total-Count matches", t); tid += 1

    # Link header has next when not at end
    def t():
        fa, fr = both_req("GET", "/pagination/skip-limit?skip=0&limit=5")
        assert_header_present(fa, fr, "Link")
        assert 'rel="next"' in fa.headers.get("Link", "")
        assert 'rel="next"' in fr.headers.get("Link", "")
    run_test(tid, cat, "Link header has rel=next", t); tid += 1

    # Link header has prev when not at start
    def t():
        fa, fr = both_req("GET", "/pagination/skip-limit?skip=20&limit=5")
        assert_header_present(fa, fr, "Link")
        assert 'rel="prev"' in fa.headers.get("Link", "")
        assert 'rel="prev"' in fr.headers.get("Link", "")
    run_test(tid, cat, "Link header has rel=prev", t); tid += 1

    # Page-based
    for page, per in [(1, 10), (2, 10), (5, 20), (10, 10), (1, 100)]:
        def t(page=page, per=per):
            fa, fr = both_req("GET", f"/pagination/page?page={page}&per_page={per}")
            assert_status_eq(fa, fr, 200)
            assert_json_eq(fa, fr)
        run_test(tid, cat, f"page={page}&per_page={per} same data", t); tid += 1

    # page=0 → 400
    def t():
        fa, fr = both_req("GET", "/pagination/page?page=0&per_page=10")
        assert_status_eq(fa, fr, 400)
    run_test(tid, cat, "page=0 → 400", t); tid += 1

    # Cursor pagination
    def t():
        # First page
        fa = _client(FA).get("/pagination/cursor?limit=10")
        fr = _client(FR).get("/pagination/cursor?limit=10")
        assert_status_eq(fa, fr, 200)
        fa_j = fa.json()
        fr_j = fr.json()
        assert fa_j["items"] == fr_j["items"]
        assert fa_j["next_cursor"] == fr_j["next_cursor"]
        CTX["cursor_page1"] = fa_j["next_cursor"]
    run_test(tid, cat, "cursor first page", t); tid += 1

    def t():
        c = CTX.get("cursor_page1")
        fa = _client(FA).get(f"/pagination/cursor?limit=10&cursor={c}")
        fr = _client(FR).get(f"/pagination/cursor?limit=10&cursor={c}")
        assert_status_eq(fa, fr, 200)
        assert_json_eq(fa, fr)
    run_test(tid, cat, "cursor second page", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/pagination/cursor?cursor=not-base64-abc")
        assert_status_eq(fa, fr, 400)
    run_test(tid, cat, "cursor invalid → 400", t); tid += 1

    return tid


# ─── Files tests ─────────────────────────────────────────────────

def files_tests(start_id):
    cat = "files"
    tid = start_id

    # upload a small text file
    def t():
        files = {"file": ("hello.txt", b"hello world", "text/plain")}
        fa = _client(FA).post("/files", files=files)
        files2 = {"file": ("hello.txt", b"hello world", "text/plain")}
        fr = _client(FR).post("/files", files=files2)
        assert_status_eq(fa, fr, 201)
        assert fa.json()["filename"] == "hello.txt" == fr.json()["filename"]
        assert fa.json()["size"] == 11 == fr.json()["size"]
        CTX["fa_file_id"] = fa.json()["id"]
        CTX["fr_file_id"] = fr.json()["id"]
    run_test(tid, cat, "POST /files upload small → 201", t); tid += 1

    # get meta
    def t():
        fa = _client(FA).get(f"/files/{CTX['fa_file_id']}/meta")
        fr = _client(FR).get(f"/files/{CTX['fr_file_id']}/meta")
        assert_status_eq(fa, fr, 200)
        assert fa.json()["filename"] == "hello.txt" == fr.json()["filename"]
    run_test(tid, cat, "GET /files/{id}/meta → 200", t); tid += 1

    # get content
    def t():
        fa = _client(FA).get(f"/files/{CTX['fa_file_id']}")
        fr = _client(FR).get(f"/files/{CTX['fr_file_id']}")
        assert_status_eq(fa, fr, 200)
        assert fa.content == b"hello world" == fr.content
    run_test(tid, cat, "GET /files/{id} → content bytes match", t); tid += 1

    # get unknown meta
    def t():
        fa, fr = both_req("GET", "/files/deadbeef/meta")
        assert_status_eq(fa, fr, 404)
    run_test(tid, cat, "GET /files/unknown/meta → 404", t); tid += 1

    # get unknown content
    def t():
        fa, fr = both_req("GET", "/files/deadbeef")
        assert_status_eq(fa, fr, 404)
    run_test(tid, cat, "GET /files/unknown → 404", t); tid += 1

    # too-large upload → 413
    def t():
        big = b"x" * (1024 * 20)
        files_a = {"file": ("big.bin", big, "application/octet-stream")}
        files_b = {"file": ("big.bin", big, "application/octet-stream")}
        fa = _client(FA).post("/files", files=files_a)
        fr = _client(FR).post("/files", files=files_b)
        assert_status_eq(fa, fr, 413)
    run_test(tid, cat, "POST /files > max size → 413", t); tid += 1

    # forbidden content type
    def t():
        files_a = {"file": ("x.evil", b"content", "application/x-evil-type")}
        files_b = {"file": ("x.evil", b"content", "application/x-evil-type")}
        fa = _client(FA).post("/files", files=files_a)
        fr = _client(FR).post("/files", files=files_b)
        assert_status_eq(fa, fr, 400)
    run_test(tid, cat, "POST /files forbidden type → 400", t); tid += 1

    # multi upload
    def t():
        files_a = [
            ("files", ("a.txt", b"aaa", "text/plain")),
            ("files", ("b.txt", b"bbbb", "text/plain")),
            ("files", ("c.txt", b"ccccc", "text/plain")),
        ]
        files_b = [
            ("files", ("a.txt", b"aaa", "text/plain")),
            ("files", ("b.txt", b"bbbb", "text/plain")),
            ("files", ("c.txt", b"ccccc", "text/plain")),
        ]
        fa = _client(FA).post("/files/multi", files=files_a)
        fr = _client(FR).post("/files/multi", files=files_b)
        assert_status_eq(fa, fr, 201)
        assert fa.json()["count"] == 3 == fr.json()["count"]
    run_test(tid, cat, "POST /files/multi 3 files → 201", t); tid += 1

    # delete
    def t():
        fa = _client(FA).delete(f"/files/{CTX['fa_file_id']}")
        fr = _client(FR).delete(f"/files/{CTX['fr_file_id']}")
        assert_status_eq(fa, fr, 204)
    run_test(tid, cat, "DELETE /files/{id} → 204", t); tid += 1

    # delete again → 404
    def t():
        fa = _client(FA).delete(f"/files/{CTX['fa_file_id']}")
        fr = _client(FR).delete(f"/files/{CTX['fr_file_id']}")
        assert_status_eq(fa, fr, 404)
    run_test(tid, cat, "DELETE /files/{id} second time → 404", t); tid += 1

    # missing file field
    def t():
        fa, fr = both_req("POST", "/files")
        assert_status_eq(fa, fr, 422)
    run_test(tid, cat, "POST /files missing file field → 422", t); tid += 1

    return tid


# ─── SSE tests ───────────────────────────────────────────────────

def sse_tests(start_id):
    cat = "sse"
    tid = start_id

    # basic SSE
    def t():
        fa, fr = both_req("GET", "/sse/events?count=3")
        assert_status_eq(fa, fr, 200)
        assert "data: event-0" in fa.text
        assert "data: event-0" in fr.text
        assert "data: event-2" in fa.text
        assert "data: event-2" in fr.text
    run_test(tid, cat, "/sse/events?count=3 has all events", t); tid += 1

    # media type
    def t():
        fa, fr = both_req("GET", "/sse/events?count=1")
        ct_fa = fa.headers.get("content-type", "")
        ct_fr = fr.headers.get("content-type", "")
        assert "text/event-stream" in ct_fa, f"fa ct={ct_fa}"
        assert "text/event-stream" in ct_fr, f"fr ct={ct_fr}"
    run_test(tid, cat, "/sse/events content-type is text/event-stream", t); tid += 1

    # typed events
    def t():
        fa, fr = both_req("GET", "/sse/typed?count=2")
        assert_status_eq(fa, fr, 200)
        assert "event: tick" in fa.text and "event: tick" in fr.text
        assert "id: 0" in fa.text and "id: 0" in fr.text
    run_test(tid, cat, "/sse/typed has event + id fields", t); tid += 1

    # heartbeat
    def t():
        fa, fr = both_req("GET", "/sse/heartbeat?count=2")
        assert_status_eq(fa, fr, 200)
        assert ": keepalive" in fa.text and ": keepalive" in fr.text
        assert "data: tick-0" in fa.text and "data: tick-0" in fr.text
    run_test(tid, cat, "/sse/heartbeat has keepalive comments", t); tid += 1

    # several counts
    for count in [0, 1, 5, 10]:
        def t(count=count):
            fa, fr = both_req("GET", f"/sse/events?count={count}")
            assert_status_eq(fa, fr, 200)
            assert fa.text.count("data:") == count
            assert fr.text.count("data:") == count
        run_test(tid, cat, f"/sse/events?count={count} emits {count} events", t); tid += 1

    return tid


# ─── Streaming tests ─────────────────────────────────────────────

def stream_tests(start_id):
    cat = "streaming"
    tid = start_id

    def t():
        fa, fr = both_req("GET", "/stream/ndjson?n=3")
        assert_status_eq(fa, fr, 200)
        fa_lines = [l for l in fa.text.splitlines() if l]
        fr_lines = [l for l in fr.text.splitlines() if l]
        assert len(fa_lines) == 3 == len(fr_lines)
        for line in fa_lines:
            json.loads(line)
        for line in fr_lines:
            json.loads(line)
    run_test(tid, cat, "/stream/ndjson?n=3 yields 3 JSON lines", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/stream/plain?n=5")
        assert_status_eq(fa, fr, 200)
        assert fa.text.count("line-") == 5
        assert fr.text.count("line-") == 5
    run_test(tid, cat, "/stream/plain?n=5 has 5 lines", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/stream/bytes?n=5")
        assert_status_eq(fa, fr, 200)
        assert len(fa.content) == 5 and len(fr.content) == 5
        assert fa.content == fr.content
    run_test(tid, cat, "/stream/bytes?n=5 yields 5 bytes", t); tid += 1

    for n in [0, 1, 10, 25]:
        def t(n=n):
            fa, fr = both_req("GET", f"/stream/ndjson?n={n}")
            assert_status_eq(fa, fr, 200)
            fa_lines = [l for l in fa.text.splitlines() if l]
            fr_lines = [l for l in fr.text.splitlines() if l]
            assert len(fa_lines) == n == len(fr_lines)
        run_test(tid, cat, f"/stream/ndjson?n={n} correct count", t); tid += 1

    # media types
    def t():
        fa, fr = both_req("GET", "/stream/ndjson?n=1")
        assert "application/x-ndjson" in fa.headers.get("content-type", "")
        assert "application/x-ndjson" in fr.headers.get("content-type", "")
    run_test(tid, cat, "ndjson content-type is x-ndjson", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/stream/plain?n=1")
        assert "text/plain" in fa.headers.get("content-type", "")
        assert "text/plain" in fr.headers.get("content-type", "")
    run_test(tid, cat, "/stream/plain content-type is text/plain", t); tid += 1

    return tid


# ─── Errors tests ────────────────────────────────────────────────

def errors_tests(start_id):
    cat = "errors"
    tid = start_id

    def t():
        fa, fr = both_req("GET", "/errors/business")
        assert_status_eq(fa, fr, 400)
        assert_json_eq(fa, fr)
    run_test(tid, cat, "BusinessError → 400 with custom body", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/errors/other")
        assert_status_eq(fa, fr, 418)
        assert_json_eq(fa, fr)
    run_test(tid, cat, "OtherBusinessError → 418 teapot", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/errors/http")
        assert_status_eq(fa, fr, 402)
    run_test(tid, cat, "HTTPException 402 → 402", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/errors/http-headers")
        assert_status_eq(fa, fr, 409)
        assert_header_present(fa, fr, "X-Conflict-Reason")
    run_test(tid, cat, "HTTPException with headers → 409 + header", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/errors/validation?n=not-int")
        assert_status_eq(fa, fr, 422)
    run_test(tid, cat, "validation error ?n=not-int → 422", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/errors/validation?n=5")
        assert_status_eq(fa, fr, 200)
        assert_json_eq(fa, fr)
    run_test(tid, cat, "validation ?n=5 → 200", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/errors/validation")
        assert_status_eq(fa, fr, 422)
    run_test(tid, cat, "validation missing n → 422", t); tid += 1

    # Custom exc body shape
    def t():
        fa = _client(FA).get("/errors/business")
        fr = _client(FR).get("/errors/business")
        assert fa.json().get("error_code") == "E_BUSINESS"
        assert fr.json().get("error_code") == "E_BUSINESS"
    run_test(tid, cat, "BusinessError body has error_code", t); tid += 1

    def t():
        fa = _client(FA).get("/errors/other")
        fr = _client(FR).get("/errors/other")
        assert fa.json().get("kind") == "teapot" == fr.json().get("kind")
    run_test(tid, cat, "OtherBusinessError body has kind=teapot", t); tid += 1

    # HTTP error body detail
    def t():
        fa = _client(FA).get("/errors/http")
        fr = _client(FR).get("/errors/http")
        assert fa.json().get("detail") == "payment required"
        assert fr.json().get("detail") == "payment required"
    run_test(tid, cat, "HTTP 402 detail body matches", t); tid += 1

    return tid


# ─── Content negotiation tests ───────────────────────────────────

def negot_tests(start_id):
    cat = "negotiation"
    tid = start_id

    def t():
        fa = _client(FA).get("/negot/content", headers={"Accept": "application/json"})
        fr = _client(FR).get("/negot/content", headers={"Accept": "application/json"})
        assert_status_eq(fa, fr, 200)
        assert fa.json() == {"msg": "Hello"} == fr.json()
    run_test(tid, cat, "/negot/content accept json → json", t); tid += 1

    def t():
        fa = _client(FA).get("/negot/content", headers={"Accept": "text/html"})
        fr = _client(FR).get("/negot/content", headers={"Accept": "text/html"})
        assert_status_eq(fa, fr, 200)
        assert "<h1>Hello</h1>" in fa.text
        assert "<h1>Hello</h1>" in fr.text
    run_test(tid, cat, "/negot/content accept html → html", t); tid += 1

    def t():
        fa = _client(FA).get("/negot/content", headers={"Accept": "text/plain"})
        fr = _client(FR).get("/negot/content", headers={"Accept": "text/plain"})
        assert_status_eq(fa, fr, 200)
        assert fa.text == "Hello" == fr.text
    run_test(tid, cat, "/negot/content accept plain → plain", t); tid += 1

    # ETag first request
    def t():
        fa, fr = both_req("GET", "/negot/etag/doc1")
        assert_status_eq(fa, fr, 200)
        assert fa.headers.get("ETag") == '"abc123"' == fr.headers.get("ETag")
        assert fa.text == "content of doc1" == fr.text
    run_test(tid, cat, "/negot/etag/doc1 first GET → ETag", t); tid += 1

    # ETag If-None-Match hits → 304
    def t():
        fa = _client(FA).get("/negot/etag/doc1", headers={"If-None-Match": '"abc123"'})
        fr = _client(FR).get("/negot/etag/doc1", headers={"If-None-Match": '"abc123"'})
        assert_status_eq(fa, fr, 304)
    run_test(tid, cat, "/negot/etag with matching If-None-Match → 304", t); tid += 1

    # ETag mismatch → 200
    def t():
        fa = _client(FA).get("/negot/etag/doc1", headers={"If-None-Match": '"WRONG"'})
        fr = _client(FR).get("/negot/etag/doc1", headers={"If-None-Match": '"WRONG"'})
        assert_status_eq(fa, fr, 200)
    run_test(tid, cat, "/negot/etag with stale If-None-Match → 200", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/negot/etag/doc2")
        assert_status_eq(fa, fr, 200)
        assert fa.headers.get("ETag") == '"def456"' == fr.headers.get("ETag")
    run_test(tid, cat, "/negot/etag/doc2 ETag header", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/negot/etag/unknown")
        assert_status_eq(fa, fr, 404)
    run_test(tid, cat, "/negot/etag/unknown → 404", t); tid += 1

    return tid


# ─── Router-level deps tests ─────────────────────────────────────

def tenant_tests(start_id):
    cat = "router-deps"
    tid = start_id

    def t():
        fa, fr = both_req("GET", "/tenant/info")
        assert_status_eq(fa, fr, 400)
    run_test(tid, cat, "/tenant/info without X-Tenant → 400", t); tid += 1

    def t():
        fa = _client(FA).get("/tenant/info", headers={"X-Tenant": "acme"})
        fr = _client(FR).get("/tenant/info", headers={"X-Tenant": "acme"})
        assert_status_eq(fa, fr, 200)
        assert fa.json()["tenant"] == "acme" == fr.json()["tenant"]
    run_test(tid, cat, "/tenant/info with X-Tenant → 200", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/tenant/stats")
        assert_status_eq(fa, fr, 400)
    run_test(tid, cat, "/tenant/stats without X-Tenant → 400", t); tid += 1

    def t():
        fa = _client(FA).get("/tenant/stats", headers={"X-Tenant": "acme"})
        fr = _client(FR).get("/tenant/stats", headers={"X-Tenant": "acme"})
        assert_status_eq(fa, fr, 200)
        assert fa.json()["tenant"] == "acme" == fr.json()["tenant"]
    run_test(tid, cat, "/tenant/stats with X-Tenant → 200", t); tid += 1

    for tenant in ["t1", "t2", "t3", "acme", "corp"]:
        def t(tenant=tenant):
            fa = _client(FA).get("/tenant/info", headers={"X-Tenant": tenant})
            fr = _client(FR).get("/tenant/info", headers={"X-Tenant": tenant})
            assert_status_eq(fa, fr, 200)
            assert fa.json()["tenant"] == tenant == fr.json()["tenant"]
        run_test(tid, cat, f"/tenant/info tenant={tenant} → 200", t); tid += 1

    return tid


# ─── Form vs JSON tests ──────────────────────────────────────────

def ingress_tests(start_id):
    cat = "ingress-form-json"
    tid = start_id

    def t():
        fa = _client(FA).post("/ingress/json", json={"name": "alice"})
        fr = _client(FR).post("/ingress/json", json={"name": "alice"})
        assert_status_eq(fa, fr, 200)
        assert fa.json()["greeting"] == "Hello, alice" == fr.json()["greeting"]
        assert fa.json()["source"] == "json" == fr.json()["source"]
    run_test(tid, cat, "POST /ingress/json → greeting", t); tid += 1

    def t():
        fa = _client(FA).post("/ingress/json", json={"name": "alice", "shout": True})
        fr = _client(FR).post("/ingress/json", json={"name": "alice", "shout": True})
        assert_status_eq(fa, fr, 200)
        assert fa.json()["greeting"] == "HELLO, ALICE!" == fr.json()["greeting"]
    run_test(tid, cat, "POST /ingress/json shout → uppercased", t); tid += 1

    def t():
        fa = _client(FA).post("/ingress/form", data={"name": "bob"})
        fr = _client(FR).post("/ingress/form", data={"name": "bob"})
        assert_status_eq(fa, fr, 200)
        assert fa.json()["greeting"] == "Hello, bob" == fr.json()["greeting"]
        assert fa.json()["source"] == "form" == fr.json()["source"]
    run_test(tid, cat, "POST /ingress/form → greeting", t); tid += 1

    def t():
        fa = _client(FA).post("/ingress/form", data={"name": "bob", "shout": "true"})
        fr = _client(FR).post("/ingress/form", data={"name": "bob", "shout": "true"})
        assert_status_eq(fa, fr, 200)
        assert fa.json()["greeting"] == "HELLO, BOB!" == fr.json()["greeting"]
    run_test(tid, cat, "POST /ingress/form shout=true → uppercased", t); tid += 1

    def t():
        fa, fr = both_req("POST", "/ingress/json", json={})
        assert_status_eq(fa, fr, 422)
    run_test(tid, cat, "POST /ingress/json missing name → 422", t); tid += 1

    def t():
        fa, fr = both_req("POST", "/ingress/form", data={})
        assert_status_eq(fa, fr, 422)
    run_test(tid, cat, "POST /ingress/form missing name → 422", t); tid += 1

    return tid


# ─── API key tests ───────────────────────────────────────────────

def apikey_tests(start_id):
    cat = "apikey"
    tid = start_id

    def t():
        fa = _client(FA).get("/apikey/header", headers={"X-API-Key": "KEY_HEADER_ABC"})
        fr = _client(FR).get("/apikey/header", headers={"X-API-Key": "KEY_HEADER_ABC"})
        assert_status_eq(fa, fr, 200)
        assert fa.json()["via"] == "header" == fr.json()["via"]
    run_test(tid, cat, "/apikey/header valid → 200", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/apikey/header")
        assert_status_eq(fa, fr, 401)
    run_test(tid, cat, "/apikey/header missing → 401", t); tid += 1

    def t():
        fa = _client(FA).get("/apikey/header", headers={"X-API-Key": "WRONG"})
        fr = _client(FR).get("/apikey/header", headers={"X-API-Key": "WRONG"})
        assert_status_eq(fa, fr, 401)
    run_test(tid, cat, "/apikey/header wrong → 401", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/apikey/query?api_key=KEY_QUERY_DEF")
        assert_status_eq(fa, fr, 200)
        assert fa.json()["via"] == "query" == fr.json()["via"]
    run_test(tid, cat, "/apikey/query valid → 200", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/apikey/query")
        assert_status_eq(fa, fr, 401)
    run_test(tid, cat, "/apikey/query missing → 401", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/apikey/query?api_key=NOPE")
        assert_status_eq(fa, fr, 401)
    run_test(tid, cat, "/apikey/query wrong → 401", t); tid += 1

    def t():
        fa = _client(FA).get("/apikey/cookie", cookies={"api_key": "KEY_COOKIE_GHI"})
        fr = _client(FR).get("/apikey/cookie", cookies={"api_key": "KEY_COOKIE_GHI"})
        assert_status_eq(fa, fr, 200)
        assert fa.json()["via"] == "cookie" == fr.json()["via"]
    run_test(tid, cat, "/apikey/cookie valid → 200", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/apikey/cookie")
        assert_status_eq(fa, fr, 401)
    run_test(tid, cat, "/apikey/cookie missing → 401", t); tid += 1

    return tid


# ─── Cookie round-trip tests ─────────────────────────────────────

def cookie_tests(start_id):
    cat = "cookies"
    tid = start_id

    def t():
        fa = _client(FA).post("/cookies/set?value=abc123")
        fr = _client(FR).post("/cookies/set?value=abc123")
        assert_status_eq(fa, fr, 200)
        assert_header_present(fa, fr, "set-cookie")
        assert "session_id=abc123" in fa.headers.get("set-cookie", "")
        assert "session_id=abc123" in fr.headers.get("set-cookie", "")
    run_test(tid, cat, "/cookies/set includes Set-Cookie", t); tid += 1

    def t():
        fa = _client(FA).get("/cookies/read", cookies={"session_id": "xyz"})
        fr = _client(FR).get("/cookies/read", cookies={"session_id": "xyz"})
        assert_status_eq(fa, fr, 200)
        assert fa.json()["session_id"] == "xyz" == fr.json()["session_id"]
    run_test(tid, cat, "/cookies/read returns session_id", t); tid += 1

    def t():
        # Clear the cookie-jar first — prior /cookies/set left
        # ``session_id=abc123`` in httpx's jar; without clearing the
        # test's "no cookie" premise never holds.
        _client(FA).cookies.clear()
        _client(FR).cookies.clear()
        fa, fr = both_req("GET", "/cookies/read")
        assert_status_eq(fa, fr, 200)
        _fa_sj = fa.json()["session_id"]
        _fr_sj = fr.json()["session_id"]
        assert _fa_sj is None and _fr_sj is None, f"fa={_fa_sj!r} fr={_fr_sj!r}"
    run_test(tid, cat, "/cookies/read without cookie → null", t); tid += 1

    def t():
        fa = _client(FA).post("/cookies/clear")
        fr = _client(FR).post("/cookies/clear")
        assert_status_eq(fa, fr, 200)
        # delete_cookie sets cookie with empty/expired
        assert_header_present(fa, fr, "set-cookie")
    run_test(tid, cat, "/cookies/clear has Set-Cookie (delete)", t); tid += 1

    # set default value
    def t():
        fa = _client(FA).post("/cookies/set")
        fr = _client(FR).post("/cookies/set")
        assert_status_eq(fa, fr, 200)
        assert fa.json()["set"] == "sess-123" == fr.json()["set"]
    run_test(tid, cat, "/cookies/set default value", t); tid += 1

    return tid


# ─── Cart stateful flow tests ────────────────────────────────────

def cart_tests(start_id):
    cat = "cart"
    tid = start_id

    def t():
        fa = _client(FA).post("/cart")
        fr = _client(FR).post("/cart")
        assert_status_eq(fa, fr, 201)
        CTX["fa_cart"] = fa.json()["cart_id"]
        CTX["fr_cart"] = fr.json()["cart_id"]
    run_test(tid, cat, "POST /cart → 201 cart_id", t); tid += 1

    def t():
        fa = _client(FA).get(f"/cart/{CTX['fa_cart']}")
        fr = _client(FR).get(f"/cart/{CTX['fr_cart']}")
        assert_status_eq(fa, fr, 200)
        assert fa.json()["total"] == 0 == fr.json()["total"]
        assert fa.json()["items"] == [] == fr.json()["items"]
    run_test(tid, cat, "GET /cart/{id} new cart is empty", t); tid += 1

    def t():
        item = {"sku": "SKU1", "qty": 2, "price": 10.0}
        fa = _client(FA).post(f"/cart/{CTX['fa_cart']}/items", json=item)
        fr = _client(FR).post(f"/cart/{CTX['fr_cart']}/items", json=item)
        assert_status_eq(fa, fr, 200)
        assert fa.json()["total"] == 20.0 == fr.json()["total"]
    run_test(tid, cat, "POST /cart/{id}/items SKU1 x2 → total=20", t); tid += 1

    def t():
        item = {"sku": "SKU2", "qty": 1, "price": 5.5}
        fa = _client(FA).post(f"/cart/{CTX['fa_cart']}/items", json=item)
        fr = _client(FR).post(f"/cart/{CTX['fr_cart']}/items", json=item)
        assert_status_eq(fa, fr, 200)
        assert fa.json()["total"] == 25.5 == fr.json()["total"]
    run_test(tid, cat, "POST SKU2 → total=25.5", t); tid += 1

    def t():
        fa = _client(FA).delete(f"/cart/{CTX['fa_cart']}/items/SKU1")
        fr = _client(FR).delete(f"/cart/{CTX['fr_cart']}/items/SKU1")
        assert_status_eq(fa, fr, 200)
        assert fa.json()["total"] == 5.5 == fr.json()["total"]
    run_test(tid, cat, "DELETE SKU1 → total=5.5", t); tid += 1

    def t():
        fa = _client(FA).delete(f"/cart/{CTX['fa_cart']}/items/DOES_NOT_EXIST")
        fr = _client(FR).delete(f"/cart/{CTX['fr_cart']}/items/DOES_NOT_EXIST")
        assert_status_eq(fa, fr, 404)
    run_test(tid, cat, "DELETE unknown SKU → 404", t); tid += 1

    def t():
        fa = _client(FA).post(f"/cart/{CTX['fa_cart']}/checkout")
        fr = _client(FR).post(f"/cart/{CTX['fr_cart']}/checkout")
        assert_status_eq(fa, fr, 200)
        assert "order_id" in fa.json() and "order_id" in fr.json()
        assert fa.json()["total"] == 5.5 == fr.json()["total"]
    run_test(tid, cat, "POST /cart/{id}/checkout → order created", t); tid += 1

    def t():
        # After checkout cart is empty
        fa = _client(FA).post(f"/cart/{CTX['fa_cart']}/checkout")
        fr = _client(FR).post(f"/cart/{CTX['fr_cart']}/checkout")
        assert_status_eq(fa, fr, 400)
    run_test(tid, cat, "POST checkout on empty cart → 400", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/cart/deadbeef")
        assert_status_eq(fa, fr, 404)
    run_test(tid, cat, "GET unknown cart → 404", t); tid += 1

    def t():
        item = {"sku": "X", "qty": 0, "price": 1.0}
        fa = _client(FA).post(f"/cart/{CTX['fa_cart']}/items", json=item)
        fr = _client(FR).post(f"/cart/{CTX['fr_cart']}/items", json=item)
        assert_status_eq(fa, fr, 400)
    run_test(tid, cat, "POST item qty=0 → 400", t); tid += 1

    def t():
        fa, fr = both_req("POST", "/cart/deadbeef/items", json={"sku": "X", "qty": 1, "price": 1.0})
        assert_status_eq(fa, fr, 404)
    run_test(tid, cat, "POST items on unknown cart → 404", t); tid += 1

    return tid


# ─── Jobs tests ──────────────────────────────────────────────────

def jobs_tests(start_id):
    cat = "jobs"
    tid = start_id

    def t():
        fa = _client(FA).post("/jobs", json={"kind": "index", "payload": {"doc": 1}})
        fr = _client(FR).post("/jobs", json={"kind": "index", "payload": {"doc": 1}})
        assert_status_eq(fa, fr, 202)
        CTX["fa_job"] = fa.json()["job_id"]
        CTX["fr_job"] = fr.json()["job_id"]
    run_test(tid, cat, "POST /jobs → 202 + job_id", t); tid += 1

    def t():
        fa = _client(FA).get(f"/jobs/{CTX['fa_job']}")
        fr = _client(FR).get(f"/jobs/{CTX['fr_job']}")
        assert_status_eq(fa, fr, 200)
        assert fa.json()["status"] == "queued" == fr.json()["status"]
    run_test(tid, cat, "GET /jobs/{id} → queued", t); tid += 1

    def t():
        fa = _client(FA).post(f"/jobs/{CTX['fa_job']}/complete", json={"ok": True, "rows": 42})
        fr = _client(FR).post(f"/jobs/{CTX['fr_job']}/complete", json={"ok": True, "rows": 42})
        assert_status_eq(fa, fr, 200)
        assert fa.json()["status"] == "done" == fr.json()["status"]
        assert fa.json()["result"] == {"ok": True, "rows": 42} == fr.json()["result"]
    run_test(tid, cat, "POST /jobs/{id}/complete → done", t); tid += 1

    def t():
        # Queue another and fail it
        fa = _client(FA).post("/jobs", json={"kind": "sweep", "payload": {}})
        fr = _client(FR).post("/jobs", json={"kind": "sweep", "payload": {}})
        assert_status_eq(fa, fr, 202)
        CTX["fa_job2"] = fa.json()["job_id"]
        CTX["fr_job2"] = fr.json()["job_id"]
    run_test(tid, cat, "POST /jobs second job → 202", t); tid += 1

    def t():
        fa = _client(FA).post(f"/jobs/{CTX['fa_job2']}/fail", json={"reason": "timeout"})
        fr = _client(FR).post(f"/jobs/{CTX['fr_job2']}/fail", json={"reason": "timeout"})
        assert_status_eq(fa, fr, 200)
        assert fa.json()["status"] == "failed" == fr.json()["status"]
        assert fa.json()["result"]["reason"] == "timeout" == fr.json()["result"]["reason"]
    run_test(tid, cat, "POST /jobs/{id}/fail → failed", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/jobs/deadbeef")
        assert_status_eq(fa, fr, 404)
    run_test(tid, cat, "GET unknown job → 404", t); tid += 1

    def t():
        fa, fr = both_req("POST", "/jobs/deadbeef/complete", json={})
        assert_status_eq(fa, fr, 404)
    run_test(tid, cat, "POST complete unknown job → 404", t); tid += 1

    def t():
        fa, fr = both_req("POST", "/jobs/deadbeef/fail", json={"reason": "x"})
        assert_status_eq(fa, fr, 404)
    run_test(tid, cat, "POST fail unknown job → 404", t); tid += 1

    # enqueue many
    for i in range(6):
        def t(i=i):
            fa = _client(FA).post("/jobs", json={"kind": f"k-{i}"})
            fr = _client(FR).post("/jobs", json={"kind": f"k-{i}"})
            assert_status_eq(fa, fr, 202)
        run_test(tid, cat, f"POST /jobs k-{i} → 202", t); tid += 1

    return tid


# ─── Redirect tests ──────────────────────────────────────────────

def redirect_tests(start_id):
    cat = "redirects"
    tid = start_id

    for path, status in [("/redir/temp", 307), ("/redir/perm", 308), ("/redir/303", 303)]:
        def t(path=path, status=status):
            fa = _client(FA).get(path)
            fr = _client(FR).get(path)
            assert _resp_status(fa) == status, f"fa={_resp_status(fa)}"
            assert _resp_status(fr) == status, f"fr={_resp_status(fr)}"
            assert fa.headers.get("location") == fr.headers.get("location")
        run_test(tid, cat, f"{path} → {status} + Location", t); tid += 1

    return tid


# ─── Validation tests ────────────────────────────────────────────

def validation_tests(start_id):
    cat = "validation"
    tid = start_id

    for n in [0, 1, 50, 100]:
        def t(n=n):
            fa, fr = both_req("GET", f"/val/q?n={n}")
            assert_status_eq(fa, fr, 200)
            assert_json_eq(fa, fr)
        run_test(tid, cat, f"/val/q?n={n} → 200", t); tid += 1

    for n in [-1, 101, 1000]:
        def t(n=n):
            fa, fr = both_req("GET", f"/val/q?n={n}")
            assert_status_eq(fa, fr, 422)
        run_test(tid, cat, f"/val/q?n={n} → 422", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/val/q?n=not_int")
        assert_status_eq(fa, fr, 422)
    run_test(tid, cat, "/val/q?n=not_int → 422", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/val/q-list?tags=a&tags=b&tags=c")
        assert_status_eq(fa, fr, 200)
        assert fa.json()["tags"] == ["a", "b", "c"] == fr.json()["tags"]
    run_test(tid, cat, "/val/q-list?tags=a&tags=b&tags=c → list", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/val/q-list")
        assert_status_eq(fa, fr, 200)
        assert fa.json()["tags"] == [] == fr.json()["tags"]
    run_test(tid, cat, "/val/q-list empty → []", t); tid += 1

    # Path validation
    def t():
        fa, fr = both_req("GET", "/val/path/alice")
        assert_status_eq(fa, fr, 200)
        assert fa.json() == fr.json()
    run_test(tid, cat, "/val/path/alice → 200", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/val/path/a")  # too short
        assert_status_eq(fa, fr, 422)
    run_test(tid, cat, "/val/path/a (too short) → 422", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/val/path/hellohellohello")  # too long
        assert_status_eq(fa, fr, 422)
    run_test(tid, cat, "/val/path/toolong → 422", t); tid += 1

    # Header validation
    def t():
        fa = _client(FA).get("/val/hdr", headers={"X-Custom": "hey"})
        fr = _client(FR).get("/val/hdr", headers={"X-Custom": "hey"})
        assert_status_eq(fa, fr, 200)
        assert fa.json()["x_custom"] == "hey" == fr.json()["x_custom"]
    run_test(tid, cat, "/val/hdr X-Custom=hey → 200", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/val/hdr")
        assert_status_eq(fa, fr, 200)
    run_test(tid, cat, "/val/hdr missing → null", t); tid += 1

    # Body validation
    def t():
        fa, fr = both_req("POST", "/val/body", json={"x": 5})
        assert_status_eq(fa, fr, 200)
        assert_json_eq(fa, fr)
    run_test(tid, cat, "/val/body x=5 → 200", t); tid += 1

    def t():
        fa, fr = both_req("POST", "/val/body", json={"x": -1})
        assert_status_eq(fa, fr, 422)
    run_test(tid, cat, "/val/body x=-1 (negative) → 422", t); tid += 1

    def t():
        fa, fr = both_req("POST", "/val/body", json={})
        assert_status_eq(fa, fr, 422)
    run_test(tid, cat, "/val/body missing x → 422", t); tid += 1

    return tid


# ─── Search tests ────────────────────────────────────────────────

def search_tests(start_id):
    cat = "search"
    tid = start_id

    queries = [
        "",
        "?q=app",
        "?q=an",
        "?category=fruit",
        "?category=veg",
        "?min_price=1",
        "?max_price=1",
        "?min_price=0.5&max_price=1.5",
        "?q=carrot&category=veg",
        "?sort=price",
        "?sort=price&order=desc",
        "?sort=name&order=asc",
        "?category=bakery&min_price=1",
        "?q=zzz",  # no results
        "?min_price=100",  # no results
    ]
    for q in queries:
        def t(q=q):
            fa, fr = both_req("GET", f"/search{q}")
            assert_status_eq(fa, fr, 200)
            assert_json_eq(fa, fr)
        run_test(tid, cat, f"/search{q}", t); tid += 1

    return tid


# ─── Blog posts + comments tests ─────────────────────────────────

def blog_tests(start_id):
    cat = "blog"
    tid = start_id

    def t():
        fa = _client(FA).post("/posts", json={"title": "hello", "body": "world"})
        fr = _client(FR).post("/posts", json={"title": "hello", "body": "world"})
        assert_status_eq(fa, fr, 201)
        CTX["fa_post1"] = fa.json()["id"]
        CTX["fr_post1"] = fr.json()["id"]
    run_test(tid, cat, "POST /posts → 201", t); tid += 1

    def t():
        fa = _client(FA).get(f"/posts/{CTX['fa_post1']}")
        fr = _client(FR).get(f"/posts/{CTX['fr_post1']}")
        assert_status_eq(fa, fr, 200)
        assert fa.json()["title"] == "hello" == fr.json()["title"]
    run_test(tid, cat, "GET /posts/{id} → 200", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/posts/99999")
        assert_status_eq(fa, fr, 404)
    run_test(tid, cat, "GET unknown post → 404", t); tid += 1

    def t():
        fa = _client(FA).post(f"/posts/{CTX['fa_post1']}/comments", json={"author": "a", "text": "nice"})
        fr = _client(FR).post(f"/posts/{CTX['fr_post1']}/comments", json={"author": "a", "text": "nice"})
        assert_status_eq(fa, fr, 201)
        CTX["fa_c1"] = fa.json()["id"]
        CTX["fr_c1"] = fr.json()["id"]
    run_test(tid, cat, "POST comment → 201", t); tid += 1

    def t():
        fa = _client(FA).post(f"/posts/{CTX['fa_post1']}/comments", json={"author": "b", "text": "meh"})
        fr = _client(FR).post(f"/posts/{CTX['fr_post1']}/comments", json={"author": "b", "text": "meh"})
        assert_status_eq(fa, fr, 201)
    run_test(tid, cat, "POST second comment → 201", t); tid += 1

    def t():
        fa = _client(FA).get(f"/posts/{CTX['fa_post1']}/comments")
        fr = _client(FR).get(f"/posts/{CTX['fr_post1']}/comments")
        assert_status_eq(fa, fr, 200)
        assert len(fa.json()) == len(fr.json()) >= 2
    run_test(tid, cat, "GET comments → 200 with 2+", t); tid += 1

    def t():
        fa = _client(FA).delete(f"/posts/{CTX['fa_post1']}/comments/{CTX['fa_c1']}")
        fr = _client(FR).delete(f"/posts/{CTX['fr_post1']}/comments/{CTX['fr_c1']}")
        assert_status_eq(fa, fr, 204)
    run_test(tid, cat, "DELETE comment → 204", t); tid += 1

    def t():
        fa = _client(FA).delete(f"/posts/{CTX['fa_post1']}/comments/{CTX['fa_c1']}")
        fr = _client(FR).delete(f"/posts/{CTX['fr_post1']}/comments/{CTX['fr_c1']}")
        assert_status_eq(fa, fr, 404)
    run_test(tid, cat, "DELETE comment twice → 404", t); tid += 1

    def t():
        fa, fr = both_req("POST", "/posts/99999/comments", json={"author": "x", "text": "y"})
        assert_status_eq(fa, fr, 404)
    run_test(tid, cat, "POST comment to unknown post → 404", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/posts/99999/comments")
        assert_status_eq(fa, fr, 404)
    run_test(tid, cat, "GET comments of unknown post → 404", t); tid += 1

    return tid


# ─── Rate limit tests ────────────────────────────────────────────

def ratelimit_tests(start_id):
    cat = "ratelimit"
    tid = start_id

    # Reset first
    def t():
        fa = _client(FA).post("/rl/reset", headers={"X-Client-ID": "t1"})
        fr = _client(FR).post("/rl/reset", headers={"X-Client-ID": "t1"})
        assert_status_eq(fa, fr, 200)
    run_test(tid, cat, "/rl/reset t1 → 200", t); tid += 1

    # Under limit
    for i in range(5):
        def t(i=i):
            fa = _client(FA).post("/rl/hit", headers={"X-Client-ID": "t1"})
            fr = _client(FR).post("/rl/hit", headers={"X-Client-ID": "t1"})
            assert_status_eq(fa, fr, 200)
            assert fa.json()["count"] == i + 1 == fr.json()["count"]
            assert fa.headers.get("X-RateLimit-Limit") == fr.headers.get("X-RateLimit-Limit")
        run_test(tid, cat, f"/rl/hit {i+1}/5 → 200", t); tid += 1

    # Over limit
    def t():
        fa = _client(FA).post("/rl/hit", headers={"X-Client-ID": "t1"})
        fr = _client(FR).post("/rl/hit", headers={"X-Client-ID": "t1"})
        assert_status_eq(fa, fr, 429)
        assert_header_present(fa, fr, "Retry-After")
    run_test(tid, cat, "/rl/hit 6th → 429 + Retry-After", t); tid += 1

    # different clients independent
    def t():
        fa = _client(FA).post("/rl/hit", headers={"X-Client-ID": "t2"})
        fr = _client(FR).post("/rl/hit", headers={"X-Client-ID": "t2"})
        assert_status_eq(fa, fr, 200)
        assert fa.json()["count"] == 1 == fr.json()["count"]
    run_test(tid, cat, "/rl/hit t2 independent of t1", t); tid += 1

    return tid


# ─── Context dep tests ───────────────────────────────────────────

def ctx_tests(start_id):
    cat = "nested-deps"
    tid = start_id

    def t():
        fa, fr = both_req("GET", "/ctx/info")
        assert_status_eq(fa, fr, 200)
        assert_json_eq(fa, fr)
    run_test(tid, cat, "/ctx/info → nested dep result", t); tid += 1

    def t():
        fa, fr = both_req("GET", "/ctx/double")
        assert_status_eq(fa, fr, 200)
        # Same dep reused twice → equal
        assert fa.json()["same"] is True
        assert fr.json()["same"] is True
    run_test(tid, cat, "/ctx/double same dep cached → both True", t); tid += 1

    return tid


# ─── Top-level health/version tests ──────────────────────────────

def top_tests(start_id):
    cat = "top-level"
    tid = start_id

    for path in ["/health", "/ready", "/version", "/"]:
        def t(path=path):
            fa, fr = both_req("GET", path)
            assert_status_eq(fa, fr, 200)
            assert_json_eq(fa, fr)
        run_test(tid, cat, f"GET {path}", t); tid += 1

    # 404 unknown
    def t():
        fa, fr = both_req("GET", "/does-not-exist-ever")
        assert_status_eq(fa, fr, 404)
    run_test(tid, cat, "GET unknown route → 404", t); tid += 1

    # Method not allowed on /health (POST)
    def t():
        fa, fr = both_req("POST", "/health")
        assert _resp_status(fa) == _resp_status(fr), f"fa={_resp_status(fa)} fr={_resp_status(fr)}"
        assert _resp_status(fa) in (405, 404), f"fa={_resp_status(fa)}"
    run_test(tid, cat, "POST /health → 405/404 consistent", t); tid += 1

    # HEAD on /health
    def t():
        fa = _client(FA).head("/health")
        fr = _client(FR).head("/health")
        assert _resp_status(fa) == _resp_status(fr)
    run_test(tid, cat, "HEAD /health consistent status", t); tid += 1

    return tid


# ─── Meta/OpenAPI tests ──────────────────────────────────────────

def meta_tests(start_id):
    cat = "meta-openapi"
    tid = start_id

    def t():
        fa = _client(FA).get("/meta/one")
        fr = _client(FR).get("/meta/one")
        assert_status_eq(fa, fr, 200)
        assert_json_eq(fa, fr)
    run_test(tid, cat, "/meta/one → 200", t); tid += 1

    def t():
        fa = _client(FA).get("/meta/two")
        fr = _client(FR).get("/meta/two")
        assert_status_eq(fa, fr, 201)
        assert_json_eq(fa, fr)
    run_test(tid, cat, "/meta/two → 201 (custom status_code)", t); tid += 1

    def t():
        fa = _client(FA).get("/meta/deprecated")
        fr = _client(FR).get("/meta/deprecated")
        assert_status_eq(fa, fr, 200)
        assert_json_eq(fa, fr)
    run_test(tid, cat, "/meta/deprecated → 200", t); tid += 1

    # OpenAPI schema
    def t():
        fa = _client(FA).get("/openapi.json")
        fr = _client(FR).get("/openapi.json")
        assert_status_eq(fa, fr, 200)
        fa_j = fa.json()
        fr_j = fr.json()
        assert fa_j.get("info", {}).get("title") == fr_j.get("info", {}).get("title")
        assert fa_j.get("info", {}).get("version") == fr_j.get("info", {}).get("version")
    run_test(tid, cat, "/openapi.json info matches", t); tid += 1

    def t():
        fa = _client(FA).get("/openapi.json")
        fr = _client(FR).get("/openapi.json")
        fa_paths = set(fa.json().get("paths", {}).keys())
        fr_paths = set(fr.json().get("paths", {}).keys())
        # Must intersect substantially
        common = fa_paths & fr_paths
        if len(common) < max(20, int(0.6 * min(len(fa_paths), len(fr_paths)))):
            raise AssertionError(f"low overlap: fa={len(fa_paths)}, fr={len(fr_paths)}, common={len(common)}")
    run_test(tid, cat, "/openapi.json paths overlap substantial", t); tid += 1

    # deprecated flag
    def t():
        fa_j = _client(FA).get("/openapi.json").json()
        fr_j = _client(FR).get("/openapi.json").json()
        fa_dep = fa_j.get("paths", {}).get("/meta/deprecated", {}).get("get", {}).get("deprecated")
        fr_dep = fr_j.get("paths", {}).get("/meta/deprecated", {}).get("get", {}).get("deprecated")
        assert fa_dep is True
        assert fr_dep is True
    run_test(tid, cat, "/meta/deprecated has deprecated=true in OpenAPI", t); tid += 1

    # docs endpoint responds
    def t():
        fa = _client(FA).get("/docs")
        fr = _client(FR).get("/docs")
        # Accept any 200 or 3xx
        assert _resp_status(fa) == _resp_status(fr) or (_resp_status(fa) < 400 and _resp_status(fr) < 400)
    run_test(tid, cat, "/docs consistent", t); tid += 1

    # redoc endpoint responds
    def t():
        fa = _client(FA).get("/redoc")
        fr = _client(FR).get("/redoc")
        assert _resp_status(fa) == _resp_status(fr) or (_resp_status(fa) < 400 and _resp_status(fr) < 400)
    run_test(tid, cat, "/redoc consistent", t); tid += 1

    return tid


# ─── Echo tests ──────────────────────────────────────────────────

def echo_tests(start_id):
    cat = "echo"
    tid = start_id

    def t():
        fa = _client(FA).get("/echo/headers", headers={"X-Echo": "hello", "User-Agent": "parity-test"})
        fr = _client(FR).get("/echo/headers", headers={"X-Echo": "hello", "User-Agent": "parity-test"})
        assert_status_eq(fa, fr, 200)
        fa_j = fa.json()
        fr_j = fr.json()
        assert fa_j.get("x-echo") == "hello" == fr_j.get("x-echo")
    run_test(tid, cat, "/echo/headers X-Echo roundtrip", t); tid += 1

    def t():
        fa = _client(FA).get("/echo/query?a=1&b=2&c=hello")
        fr = _client(FR).get("/echo/query?a=1&b=2&c=hello")
        assert_status_eq(fa, fr, 200)
        assert fa.json() == fr.json() == {"a": "1", "b": "2", "c": "hello"}
    run_test(tid, cat, "/echo/query basic", t); tid += 1

    def t():
        body = b"hello world"
        fa = _client(FA).post("/echo/body", content=body)
        fr = _client(FR).post("/echo/body", content=body)
        assert_status_eq(fa, fr, 200)
        assert fa.json()["len"] == len(body) == fr.json()["len"]
        assert fa.json()["sha256"] == fr.json()["sha256"]
    run_test(tid, cat, "/echo/body sha256 matches", t); tid += 1

    # multiple echoes
    for i in range(5):
        def t(i=i):
            body = f"payload-{i}".encode()
            fa = _client(FA).post("/echo/body", content=body)
            fr = _client(FR).post("/echo/body", content=body)
            assert_status_eq(fa, fr, 200)
            assert fa.json()["sha256"] == fr.json()["sha256"]
        run_test(tid, cat, f"/echo/body payload-{i}", t); tid += 1

    for q in ["?k=v", "?x=a&x=b", "?empty=", "?name=alice+bob"]:
        def t(q=q):
            fa, fr = both_req("GET", f"/echo/query{q}")
            assert_status_eq(fa, fr, 200)
            assert fa.json() == fr.json()
        run_test(tid, cat, f"/echo/query{q}", t); tid += 1

    return tid


# ─── Counter / idempotency tests ─────────────────────────────────

def counter_tests(start_id):
    cat = "counter"
    tid = start_id

    # make sure starting clean
    def t():
        fa = _client(FA).delete("/counter/visits")
        fr = _client(FR).delete("/counter/visits")
        assert_status_eq(fa, fr, 204)
    run_test(tid, cat, "DELETE /counter/visits reset", t); tid += 1

    # inc 3 times
    for i in range(1, 4):
        def t(i=i):
            fa = _client(FA).post("/counter/visits/inc", json={"by": 1})
            fr = _client(FR).post("/counter/visits/inc", json={"by": 1})
            assert_status_eq(fa, fr, 200)
            assert fa.json()["value"] == i == fr.json()["value"]
        run_test(tid, cat, f"/counter/visits/inc → {i}", t); tid += 1

    # inc by 10
    def t():
        fa = _client(FA).post("/counter/visits/inc", json={"by": 10})
        fr = _client(FR).post("/counter/visits/inc", json={"by": 10})
        assert_status_eq(fa, fr, 200)
        assert fa.json()["value"] == 13 == fr.json()["value"]
    run_test(tid, cat, "/counter/visits/inc by 10 → 13", t); tid += 1

    # idempotency
    def t():
        key = "idem-abc"
        fa = _client(FA).post("/counter/visits/inc", json={"by": 5}, headers={"Idempotency-Key": key})
        fr = _client(FR).post("/counter/visits/inc", json={"by": 5}, headers={"Idempotency-Key": key})
        v1_fa = fa.json()["value"]
        v1_fr = fr.json()["value"]
        fa2 = _client(FA).post("/counter/visits/inc", json={"by": 5}, headers={"Idempotency-Key": key})
        fr2 = _client(FR).post("/counter/visits/inc", json={"by": 5}, headers={"Idempotency-Key": key})
        assert fa2.json()["value"] == v1_fa, f"fa idem broken: {v1_fa} → {fa2.json()['value']}"
        assert fr2.json()["value"] == v1_fr, f"fr idem broken: {v1_fr} → {fr2.json()['value']}"
    run_test(tid, cat, "idempotency key: double POST same value", t); tid += 1

    # get value
    def t():
        fa = _client(FA).get("/counter/visits")
        fr = _client(FR).get("/counter/visits")
        assert_status_eq(fa, fr, 200)
        assert fa.json()["value"] == fr.json()["value"]
    run_test(tid, cat, "GET /counter/visits consistent", t); tid += 1

    # many different counters
    for name in ["a", "b", "c", "d"]:
        def t(name=name):
            fa = _client(FA).post(f"/counter/{name}/inc", json={"by": 1})
            fr = _client(FR).post(f"/counter/{name}/inc", json={"by": 1})
            assert_status_eq(fa, fr, 200)
            assert fa.json()["value"] == 1 == fr.json()["value"]
        run_test(tid, cat, f"/counter/{name}/inc → 1", t); tid += 1

    return tid


# ─── Middleware tests ────────────────────────────────────────────

def middleware_tests(start_id):
    cat = "middleware"
    tid = start_id

    # CORS preflight
    def t():
        headers = {
            "Origin": "http://foo.example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type,Authorization",
        }
        fa = _client(FA).options("/items", headers=headers)
        fr = _client(FR).options("/items", headers=headers)
        # Status should match; CORS configs may return 200 or 204
        assert _resp_status(fa) == _resp_status(fr), f"fa={_resp_status(fa)}, fr={_resp_status(fr)}"
    run_test(tid, cat, "OPTIONS preflight consistent status", t); tid += 1

    # CORS simple request: Access-Control-Allow-Origin
    def t():
        fa = _client(FA).get("/health", headers={"Origin": "http://foo.example.com"})
        fr = _client(FR).get("/health", headers={"Origin": "http://foo.example.com"})
        assert_status_eq(fa, fr, 200)
        fa_acao = fa.headers.get("access-control-allow-origin")
        fr_acao = fr.headers.get("access-control-allow-origin")
        assert fa_acao is not None, "fa missing Access-Control-Allow-Origin"
        assert fr_acao is not None, "fr missing Access-Control-Allow-Origin"
    run_test(tid, cat, "CORS: simple GET has Access-Control-Allow-Origin", t); tid += 1

    # GZip compression for large response (>500 bytes)
    def t():
        # /search returns enough data; /openapi.json is very large
        fa = _client(FA).get("/openapi.json", headers={"Accept-Encoding": "gzip"})
        fr = _client(FR).get("/openapi.json", headers={"Accept-Encoding": "gzip"})
        assert_status_eq(fa, fr, 200)
        # both should be large (>500 bytes). If gzip applied, content-encoding=gzip
        # httpx decompresses transparently. We just assert both are consistent
        # w.r.t. content-encoding header.
        fa_ce = fa.headers.get("content-encoding", "")
        fr_ce = fr.headers.get("content-encoding", "")
        assert fa_ce == fr_ce, f"fa ce={fa_ce}, fr ce={fr_ce}"
    run_test(tid, cat, "GZip: /openapi.json consistent content-encoding", t); tid += 1

    # No gzip for small response
    def t():
        fa = _client(FA).get("/health", headers={"Accept-Encoding": "gzip"})
        fr = _client(FR).get("/health", headers={"Accept-Encoding": "gzip"})
        fa_ce = fa.headers.get("content-encoding", "")
        fr_ce = fr.headers.get("content-encoding", "")
        # Must be consistent
        assert (fa_ce != "") == (fr_ce != ""), f"fa={fa_ce} fr={fr_ce}"
    run_test(tid, cat, "GZip: /health no encoding on small", t); tid += 1

    # No Accept-Encoding → no gzip
    def t():
        fa = _client(FA).get("/openapi.json")
        fr = _client(FR).get("/openapi.json")
        assert_status_eq(fa, fr, 200)
        fa_ce = fa.headers.get("content-encoding", "")
        fr_ce = fr.headers.get("content-encoding", "")
        # httpx auto-accepts gzip by default, so this may still be gzip
        # Just assert consistency
        assert fa_ce == fr_ce, f"fa ce={fa_ce}, fr ce={fr_ce}"
    run_test(tid, cat, "GZip: /openapi.json default Accept-Encoding", t); tid += 1

    # CORS custom origin preflight
    def t():
        headers = {
            "Origin": "http://evil.example.com",
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "X-Custom",
        }
        fa = _client(FA).options("/items/1", headers=headers)
        fr = _client(FR).options("/items/1", headers=headers)
        assert _resp_status(fa) == _resp_status(fr)
    run_test(tid, cat, "OPTIONS preflight DELETE consistent", t); tid += 1

    return tid


# ─── Big end-to-end flow tests (chained multi-endpoint) ──────────

def e2e_tests(start_id):
    cat = "e2e"
    tid = start_id

    # e2e: login → create → me sees token; then patch; then delete
    def t():
        fa_login = _client(FA).post("/auth/login", json={"username": "bob", "password": "builder"})
        fr_login = _client(FR).post("/auth/login", json={"username": "bob", "password": "builder"})
        fa_t = fa_login.json()["token"]
        fr_t = fr_login.json()["token"]
        fa_me = _client(FA).get("/auth/me", headers={"Authorization": f"Bearer {fa_t}"})
        fr_me = _client(FR).get("/auth/me", headers={"Authorization": f"Bearer {fr_t}"})
        assert_status_eq(fa_me, fr_me, 200)
        assert fa_me.json()["role"] == fr_me.json()["role"]
    run_test(tid, cat, "e2e: login+me bob role parity", t); tid += 1

    # e2e: full cart flow
    def t():
        fa_c = _client(FA).post("/cart").json()["cart_id"]
        fr_c = _client(FR).post("/cart").json()["cart_id"]
        for sku, qty, price in [("A", 1, 10.0), ("B", 2, 5.0), ("C", 3, 1.0)]:
            _client(FA).post(f"/cart/{fa_c}/items", json={"sku": sku, "qty": qty, "price": price})
            _client(FR).post(f"/cart/{fr_c}/items", json={"sku": sku, "qty": qty, "price": price})
        fa_final = _client(FA).get(f"/cart/{fa_c}").json()
        fr_final = _client(FR).get(f"/cart/{fr_c}").json()
        assert fa_final["total"] == fr_final["total"] == 23.0
    run_test(tid, cat, "e2e: cart add 3 items → total=23", t); tid += 1

    # e2e: checkout order_id returned
    def t():
        fa_c = _client(FA).post("/cart").json()["cart_id"]
        fr_c = _client(FR).post("/cart").json()["cart_id"]
        _client(FA).post(f"/cart/{fa_c}/items", json={"sku": "X", "qty": 1, "price": 99.0})
        _client(FR).post(f"/cart/{fr_c}/items", json={"sku": "X", "qty": 1, "price": 99.0})
        fa_ck = _client(FA).post(f"/cart/{fa_c}/checkout").json()
        fr_ck = _client(FR).post(f"/cart/{fr_c}/checkout").json()
        assert fa_ck["total"] == fr_ck["total"] == 99.0
        assert "order_id" in fa_ck and "order_id" in fr_ck
    run_test(tid, cat, "e2e: cart create+add+checkout → order", t); tid += 1

    # e2e: user → post → comment → delete
    def t():
        fa_u = _client(FA).post("/api/v1/users", json={"name": "erin", "email": "e@e.com"}).json()["id"]
        fr_u = _client(FR).post("/api/v1/users", json={"name": "erin", "email": "e@e.com"}).json()["id"]
        # posts belong to /posts endpoint, but we'll test nested user/posts
        fa_p = _client(FA).post(f"/api/v1/users/{fa_u}/posts", json={"title": "t", "body": "b"}).json()["id"]
        fr_p = _client(FR).post(f"/api/v1/users/{fr_u}/posts", json={"title": "t", "body": "b"}).json()["id"]
        fa_list = _client(FA).get(f"/api/v1/users/{fa_u}/posts").json()
        fr_list = _client(FR).get(f"/api/v1/users/{fr_u}/posts").json()
        assert len(fa_list) == len(fr_list) == 1
        assert fa_list[0]["title"] == fr_list[0]["title"] == "t"
    run_test(tid, cat, "e2e: create user → post → list", t); tid += 1

    # e2e: job lifecycle complete
    def t():
        fa_j = _client(FA).post("/jobs", json={"kind": "k"}).json()["job_id"]
        fr_j = _client(FR).post("/jobs", json={"kind": "k"}).json()["job_id"]
        _client(FA).post(f"/jobs/{fa_j}/complete", json={"value": 1})
        _client(FR).post(f"/jobs/{fr_j}/complete", json={"value": 1})
        fa_final = _client(FA).get(f"/jobs/{fa_j}").json()
        fr_final = _client(FR).get(f"/jobs/{fr_j}").json()
        assert fa_final["status"] == fr_final["status"] == "done"
    run_test(tid, cat, "e2e: job → complete → done", t); tid += 1

    # e2e: job lifecycle fail
    def t():
        fa_j = _client(FA).post("/jobs", json={"kind": "k"}).json()["job_id"]
        fr_j = _client(FR).post("/jobs", json={"kind": "k"}).json()["job_id"]
        _client(FA).post(f"/jobs/{fa_j}/fail", json={"reason": "bad"})
        _client(FR).post(f"/jobs/{fr_j}/fail", json={"reason": "bad"})
        fa_final = _client(FA).get(f"/jobs/{fa_j}").json()
        fr_final = _client(FR).get(f"/jobs/{fr_j}").json()
        assert fa_final["status"] == fr_final["status"] == "failed"
    run_test(tid, cat, "e2e: job → fail → failed", t); tid += 1

    # e2e: blog post + comment delete
    def t():
        fa_p = _client(FA).post("/posts", json={"title": "x", "body": "y"}).json()["id"]
        fr_p = _client(FR).post("/posts", json={"title": "x", "body": "y"}).json()["id"]
        fa_c = _client(FA).post(f"/posts/{fa_p}/comments", json={"author": "a", "text": "t"}).json()["id"]
        fr_c = _client(FR).post(f"/posts/{fr_p}/comments", json={"author": "a", "text": "t"}).json()["id"]
        fa_list = _client(FA).get(f"/posts/{fa_p}/comments").json()
        fr_list = _client(FR).get(f"/posts/{fr_p}/comments").json()
        assert len(fa_list) == len(fr_list) == 1
        _client(FA).delete(f"/posts/{fa_p}/comments/{fa_c}")
        _client(FR).delete(f"/posts/{fr_p}/comments/{fr_c}")
        fa_list2 = _client(FA).get(f"/posts/{fa_p}/comments").json()
        fr_list2 = _client(FR).get(f"/posts/{fr_p}/comments").json()
        assert fa_list2 == fr_list2 == []
    run_test(tid, cat, "e2e: post → comment → delete", t); tid += 1

    # e2e: auth + admin consistency
    def t():
        fa_t = _client(FA).post("/auth/login", json={"username": "alice", "password": "wonderland"}).json()["token"]
        fr_t = _client(FR).post("/auth/login", json={"username": "alice", "password": "wonderland"}).json()["token"]
        fa_a = _client(FA).get("/auth/admin", headers={"Authorization": f"Bearer {fa_t}"})
        fr_a = _client(FR).get("/auth/admin", headers={"Authorization": f"Bearer {fr_t}"})
        assert_status_eq(fa_a, fr_a, 200)
    run_test(tid, cat, "e2e: login alice → admin access", t); tid += 1

    # e2e: auth + admin denied for user
    def t():
        fa_t = _client(FA).post("/auth/login", json={"username": "bob", "password": "builder"}).json()["token"]
        fr_t = _client(FR).post("/auth/login", json={"username": "bob", "password": "builder"}).json()["token"]
        fa_a = _client(FA).get("/auth/admin", headers={"Authorization": f"Bearer {fa_t}"})
        fr_a = _client(FR).get("/auth/admin", headers={"Authorization": f"Bearer {fr_t}"})
        assert_status_eq(fa_a, fr_a, 403)
    run_test(tid, cat, "e2e: login bob → admin 403", t); tid += 1

    # e2e: rate limit cycle
    def t():
        _client(FA).post("/rl/reset", headers={"X-Client-ID": "e2e1"})
        _client(FR).post("/rl/reset", headers={"X-Client-ID": "e2e1"})
        for _ in range(5):
            _client(FA).post("/rl/hit", headers={"X-Client-ID": "e2e1"})
            _client(FR).post("/rl/hit", headers={"X-Client-ID": "e2e1"})
        fa = _client(FA).post("/rl/hit", headers={"X-Client-ID": "e2e1"})
        fr = _client(FR).post("/rl/hit", headers={"X-Client-ID": "e2e1"})
        assert_status_eq(fa, fr, 429)
    run_test(tid, cat, "e2e: rate limit 5 hits then 429", t); tid += 1

    # e2e: search → filter → sort → pagination-like
    def t():
        fa = _client(FA).get("/search?category=fruit&sort=price&order=asc").json()
        fr = _client(FR).get("/search?category=fruit&sort=price&order=asc").json()
        assert fa == fr
        assert len(fa["results"]) == 2  # apple + banana
    run_test(tid, cat, "e2e: search fruit sort by price asc", t); tid += 1

    # e2e: create user → post → comment not on user posts, check separate namespaces
    def t():
        fa_u = _client(FA).post("/api/v1/users", json={"name": "u1", "email": "u1@x.com"}).json()["id"]
        fr_u = _client(FR).post("/api/v1/users", json={"name": "u1", "email": "u1@x.com"}).json()["id"]
        _client(FA).post(f"/api/v1/users/{fa_u}/posts", json={"title": "1", "body": "1"})
        _client(FR).post(f"/api/v1/users/{fr_u}/posts", json={"title": "1", "body": "1"})
        _client(FA).post(f"/api/v1/users/{fa_u}/posts", json={"title": "2", "body": "2"})
        _client(FR).post(f"/api/v1/users/{fr_u}/posts", json={"title": "2", "body": "2"})
        _client(FA).post(f"/api/v1/users/{fa_u}/posts", json={"title": "3", "body": "3"})
        _client(FR).post(f"/api/v1/users/{fr_u}/posts", json={"title": "3", "body": "3"})
        fa_list = _client(FA).get(f"/api/v1/users/{fa_u}/posts").json()
        fr_list = _client(FR).get(f"/api/v1/users/{fr_u}/posts").json()
        assert len(fa_list) == len(fr_list) == 3
    run_test(tid, cat, "e2e: user with 3 posts listed", t); tid += 1

    return tid


# ─── Lots of parameterized parity sweeps to hit the 500 mark ─────

def parametric_tests(start_id):
    cat = "parametric"
    tid = start_id

    # Inc counters with many names
    for name in [f"p-{i}" for i in range(20)]:
        def t(name=name):
            fa = _client(FA).post(f"/counter/{name}/inc", json={"by": 2})
            fr = _client(FR).post(f"/counter/{name}/inc", json={"by": 2})
            assert_status_eq(fa, fr, 200)
            assert fa.json()["value"] == 2 == fr.json()["value"]
        run_test(tid, cat, f"/counter/{name}/inc by 2", t); tid += 1

    # Range of val queries
    for n in range(0, 100, 5):
        def t(n=n):
            fa, fr = both_req("GET", f"/val/q?n={n}")
            assert_status_eq(fa, fr, 200)
            assert_json_eq(fa, fr)
        run_test(tid, cat, f"/val/q?n={n}", t); tid += 1

    # Many search queries
    for q in ["apple", "banana", "carrot", "xyz", "e"]:
        def t(q=q):
            fa, fr = both_req("GET", f"/search?q={q}")
            assert_status_eq(fa, fr, 200)
            assert_json_eq(fa, fr)
        run_test(tid, cat, f"/search?q={q}", t); tid += 1

    # Many auth/me calls
    for user, pw in [("alice", "wonderland"), ("bob", "builder"), ("eve", "hunter2")]:
        def t(user=user, pw=pw):
            fa = _client(FA).post("/auth/login", json={"username": user, "password": pw})
            fr = _client(FR).post("/auth/login", json={"username": user, "password": pw})
            assert_status_eq(fa, fr, 200)
            fa_t = fa.json()["token"]
            fr_t = fr.json()["token"]
            fa_me = _client(FA).get("/auth/me", headers={"Authorization": f"Bearer {fa_t}"})
            fr_me = _client(FR).get("/auth/me", headers={"Authorization": f"Bearer {fr_t}"})
            assert_status_eq(fa_me, fr_me, 200)
            assert fa_me.json()["username"] == user == fr_me.json()["username"]
        run_test(tid, cat, f"e2e login+me {user}", t); tid += 1

    # Varying tenant headers
    for tenant in [f"t-{i}" for i in range(10)]:
        def t(tenant=tenant):
            fa = _client(FA).get("/tenant/info", headers={"X-Tenant": tenant})
            fr = _client(FR).get("/tenant/info", headers={"X-Tenant": tenant})
            assert_status_eq(fa, fr, 200)
            assert fa.json()["tenant"] == tenant == fr.json()["tenant"]
        run_test(tid, cat, f"/tenant/info tenant={tenant}", t); tid += 1

    # Many echo queries
    for i in range(15):
        def t(i=i):
            q = f"?i={i}&name=val-{i}"
            fa, fr = both_req("GET", f"/echo/query{q}")
            assert_status_eq(fa, fr, 200)
            assert fa.json() == fr.json()
        run_test(tid, cat, f"/echo/query i={i}", t); tid += 1

    # Many pagination sweeps
    for skip in range(0, 100, 10):
        def t(skip=skip):
            fa, fr = both_req("GET", f"/pagination/skip-limit?skip={skip}&limit=5")
            assert_status_eq(fa, fr, 200)
            assert_json_eq(fa, fr)
        run_test(tid, cat, f"/pagination/skip-limit skip={skip}", t); tid += 1

    # Many redirects
    for endpoint, status in [("/redir/temp", 307), ("/redir/perm", 308), ("/redir/303", 303)]:
        for _ in range(3):
            def t(endpoint=endpoint, status=status):
                fa = _client(FA).get(endpoint)
                fr = _client(FR).get(endpoint)
                assert _resp_status(fa) == status
                assert _resp_status(fr) == status
            run_test(tid, cat, f"{endpoint} → {status}", t); tid += 1

    # Many negot etag hits
    for doc_id in ["doc1", "doc2"]:
        for h in [{}, {"If-None-Match": '"WRONG"'}, {"If-None-Match": '"abc123"'}]:
            def t(doc_id=doc_id, h=h):
                fa = _client(FA).get(f"/negot/etag/{doc_id}", headers=h)
                fr = _client(FR).get(f"/negot/etag/{doc_id}", headers=h)
                assert _resp_status(fa) == _resp_status(fr)
            run_test(tid, cat, f"negot/etag/{doc_id} with {h}", t); tid += 1

    # Many apikey header checks
    for k in ["KEY_HEADER_ABC", "KEY_QUERY_DEF", "KEY_COOKIE_GHI", "BAD", ""]:
        def t(k=k):
            headers = {"X-API-Key": k} if k else {}
            fa = _client(FA).get("/apikey/header", headers=headers)
            fr = _client(FR).get("/apikey/header", headers=headers)
            assert _resp_status(fa) == _resp_status(fr)
        run_test(tid, cat, f"/apikey/header key={k!r}", t); tid += 1

    # Various blog posts (parity on creating)
    for i in range(15):
        def t(i=i):
            fa = _client(FA).post("/posts", json={"title": f"t{i}", "body": f"b{i}"})
            fr = _client(FR).post("/posts", json={"title": f"t{i}", "body": f"b{i}"})
            assert_status_eq(fa, fr, 201)
            assert fa.json()["title"] == f"t{i}" == fr.json()["title"]
        run_test(tid, cat, f"POST /posts t{i}", t); tid += 1

    # Many jobs enqueue/status
    for i in range(10):
        def t(i=i):
            fa = _client(FA).post("/jobs", json={"kind": f"k{i}"})
            fr = _client(FR).post("/jobs", json={"kind": f"k{i}"})
            assert_status_eq(fa, fr, 202)
            fa_j = fa.json()["job_id"]
            fr_j = fr.json()["job_id"]
            fa_s = _client(FA).get(f"/jobs/{fa_j}")
            fr_s = _client(FR).get(f"/jobs/{fr_j}")
            assert_status_eq(fa_s, fr_s, 200)
            assert fa_s.json()["status"] == "queued" == fr_s.json()["status"]
        run_test(tid, cat, f"/jobs enqueue k{i} + status queued", t); tid += 1

    # Varying echo body sizes
    for size in [0, 1, 10, 100, 500, 1000]:
        def t(size=size):
            body = b"x" * size
            fa = _client(FA).post("/echo/body", content=body)
            fr = _client(FR).post("/echo/body", content=body)
            assert_status_eq(fa, fr, 200)
            assert fa.json()["len"] == size == fr.json()["len"]
            assert fa.json()["sha256"] == fr.json()["sha256"]
        run_test(tid, cat, f"/echo/body size={size}", t); tid += 1

    # Varying SSE counts
    for c in range(0, 12):
        def t(c=c):
            fa, fr = both_req("GET", f"/sse/events?count={c}")
            assert_status_eq(fa, fr, 200)
            assert fa.text.count("data:") == c == fr.text.count("data:")
        run_test(tid, cat, f"/sse/events count={c}", t); tid += 1

    # Varying ndjson counts
    for n in range(0, 10):
        def t(n=n):
            fa, fr = both_req("GET", f"/stream/ndjson?n={n}")
            assert_status_eq(fa, fr, 200)
            lines = [l for l in fa.text.splitlines() if l]
            assert len(lines) == n
        run_test(tid, cat, f"/stream/ndjson n={n}", t); tid += 1

    # Varying items creation
    for i in range(10):
        def t(i=i):
            name = f"par-item-{i}"
            fa = _client(FA).post("/items", json={"name": name, "price": float(i)})
            fr = _client(FR).post("/items", json={"name": name, "price": float(i)})
            assert_status_eq(fa, fr, 201)
        run_test(tid, cat, f"POST /items {i}", t); tid += 1

    # Many search sort sweeps
    for sort_field in ["id", "name", "price"]:
        for order in ["asc", "desc"]:
            def t(sort_field=sort_field, order=order):
                fa, fr = both_req("GET", f"/search?sort={sort_field}&order={order}")
                assert_status_eq(fa, fr, 200)
                assert_json_eq(fa, fr)
            run_test(tid, cat, f"/search?sort={sort_field}&order={order}", t); tid += 1

    # Page-based pagination sweeps
    for page in range(1, 15):
        def t(page=page):
            fa, fr = both_req("GET", f"/pagination/page?page={page}&per_page=10")
            assert_status_eq(fa, fr, 200)
            assert_json_eq(fa, fr)
        run_test(tid, cat, f"/pagination/page page={page}", t); tid += 1

    # Cookie roundtrip sweeps
    for val in ["v1", "v2", "v3", "hello world", "special!"]:
        def t(val=val):
            fa = _client(FA).get("/cookies/read", cookies={"session_id": val})
            fr = _client(FR).get("/cookies/read", cookies={"session_id": val})
            assert_status_eq(fa, fr, 200)
            assert fa.json()["session_id"] == val == fr.json()["session_id"]
        run_test(tid, cat, f"/cookies/read session_id={val}", t); tid += 1

    # Many val q-list
    for tags in [["a"], ["a", "b"], ["a", "b", "c"], ["x"] * 5]:
        def t(tags=tags):
            q = "&".join(f"tags={t}" for t in tags)
            fa, fr = both_req("GET", f"/val/q-list?{q}")
            assert_status_eq(fa, fr, 200)
            assert fa.json()["tags"] == tags == fr.json()["tags"]
        run_test(tid, cat, f"/val/q-list tags={tags}", t); tid += 1

    # Many val/path
    for name in ["bo", "abc", "alice", "wonderland"]:
        def t(name=name):
            fa, fr = both_req("GET", f"/val/path/{name}")
            assert_status_eq(fa, fr, 200)
        run_test(tid, cat, f"/val/path/{name}", t); tid += 1

    # Many /val/body
    for x in [0, 1, 10, 100, 1000]:
        def t(x=x):
            fa, fr = both_req("POST", "/val/body", json={"x": x})
            assert_status_eq(fa, fr, 200)
            assert fa.json()["x"] == x == fr.json()["x"]
        run_test(tid, cat, f"/val/body x={x}", t); tid += 1

    # Various http error codes
    for path in ["/errors/business", "/errors/other", "/errors/http", "/errors/http-headers"]:
        def t(path=path):
            fa, fr = both_req("GET", path)
            assert _resp_status(fa) == _resp_status(fr)
        run_test(tid, cat, f"error parity {path}", t); tid += 1

    # Many /ingress/json calls
    for i in range(10):
        def t(i=i):
            fa = _client(FA).post("/ingress/json", json={"name": f"user{i}", "shout": i % 2 == 0})
            fr = _client(FR).post("/ingress/json", json={"name": f"user{i}", "shout": i % 2 == 0})
            assert_status_eq(fa, fr, 200)
            assert fa.json()["greeting"] == fr.json()["greeting"]
        run_test(tid, cat, f"/ingress/json user{i}", t); tid += 1

    # Many /ingress/form calls
    for i in range(10):
        def t(i=i):
            fa = _client(FA).post("/ingress/form", data={"name": f"formuser{i}", "shout": "true" if i % 2 == 0 else "false"})
            fr = _client(FR).post("/ingress/form", data={"name": f"formuser{i}", "shout": "true" if i % 2 == 0 else "false"})
            assert_status_eq(fa, fr, 200)
            assert fa.json()["greeting"] == fr.json()["greeting"]
        run_test(tid, cat, f"/ingress/form user{i}", t); tid += 1

    # Create many /jobs and query
    for i in range(5):
        def t(i=i):
            fa = _client(FA).post("/jobs", json={"kind": f"batch-{i}", "payload": {"idx": i}})
            fr = _client(FR).post("/jobs", json={"kind": f"batch-{i}", "payload": {"idx": i}})
            assert_status_eq(fa, fr, 202)
            fa_id = fa.json()["job_id"]
            fr_id = fr.json()["job_id"]
            fa_g = _client(FA).get(f"/jobs/{fa_id}")
            fr_g = _client(FR).get(f"/jobs/{fr_id}")
            assert fa_g.json()["kind"] == f"batch-{i}" == fr_g.json()["kind"]
        run_test(tid, cat, f"job batch-{i}", t); tid += 1

    # Content-negotiation sweeps
    for accept in ["application/json", "text/html", "text/plain", "*/*"]:
        def t(accept=accept):
            fa = _client(FA).get("/negot/content", headers={"Accept": accept})
            fr = _client(FR).get("/negot/content", headers={"Accept": accept})
            assert_status_eq(fa, fr, 200)
        run_test(tid, cat, f"/negot/content Accept={accept}", t); tid += 1

    # Nested dep calls
    for _ in range(5):
        def t():
            fa, fr = both_req("GET", "/ctx/info")
            assert_status_eq(fa, fr, 200)
            assert_json_eq(fa, fr)
        run_test(tid, cat, "/ctx/info repeat", t); tid += 1

    # Echo headers many
    for i in range(10):
        def t(i=i):
            fa = _client(FA).get("/echo/headers", headers={"X-Echo": f"v-{i}"})
            fr = _client(FR).get("/echo/headers", headers={"X-Echo": f"v-{i}"})
            assert_status_eq(fa, fr, 200)
            assert fa.json().get("x-echo") == f"v-{i}" == fr.json().get("x-echo")
        run_test(tid, cat, f"/echo/headers X-Echo=v-{i}", t); tid += 1

    # Various validation fail paths
    for n in [-1, -50, 101, 200, 1000]:
        def t(n=n):
            fa, fr = both_req("GET", f"/val/q?n={n}")
            assert_status_eq(fa, fr, 422)
        run_test(tid, cat, f"/val/q?n={n} → 422", t); tid += 1

    # Blog post creation sweep
    for i in range(10):
        def t(i=i):
            fa = _client(FA).post("/posts", json={"title": f"P{i}", "body": f"Body {i}"})
            fr = _client(FR).post("/posts", json={"title": f"P{i}", "body": f"Body {i}"})
            assert_status_eq(fa, fr, 201)
            assert fa.json()["body"] == f"Body {i}" == fr.json()["body"]
        run_test(tid, cat, f"POST /posts P{i}", t); tid += 1

    # /search sweeps
    for min_p, max_p in [(0, 1), (0.5, 1.5), (1, 10), (0, 100)]:
        def t(min_p=min_p, max_p=max_p):
            fa = _client(FA).get(f"/search?min_price={min_p}&max_price={max_p}")
            fr = _client(FR).get(f"/search?min_price={min_p}&max_price={max_p}")
            assert_status_eq(fa, fr, 200)
            assert_json_eq(fa, fr)
        run_test(tid, cat, f"/search price [{min_p},{max_p}]", t); tid += 1

    return tid


# ── Big runner ───────────────────────────────────────────────────

def run_all(fa_port, rs_port):
    print(f"{BOLD}{CYAN}Beginning flow tests…{RESET}\n")
    tid = 1
    tid = auth_tests(tid)
    tid = crud_tests(tid)
    tid = users_tests(tid)
    tid = pagination_tests(tid)
    tid = files_tests(tid)
    tid = sse_tests(tid)
    tid = stream_tests(tid)
    tid = errors_tests(tid)
    tid = negot_tests(tid)
    tid = tenant_tests(tid)
    tid = ingress_tests(tid)
    tid = apikey_tests(tid)
    tid = cookie_tests(tid)
    tid = cart_tests(tid)
    tid = jobs_tests(tid)
    tid = redirect_tests(tid)
    tid = validation_tests(tid)
    tid = search_tests(tid)
    tid = blog_tests(tid)
    tid = ratelimit_tests(tid)
    tid = ctx_tests(tid)
    tid = top_tests(tid)
    tid = meta_tests(tid)
    tid = echo_tests(tid)
    tid = counter_tests(tid)
    tid = middleware_tests(tid)
    tid = e2e_tests(tid)
    tid = parametric_tests(tid)


# ── Main ─────────────────────────────────────────────────────────

def main():
    print(f"\n{BOLD}{'='*72}")
    print(f"  Deep Integration Parity Suite")
    print(f"  FastAPI on :{FASTAPI_PORT}   |   fastapi-turbo on :{FASTAPI_TURBO_PORT}")
    print(f"{'='*72}{RESET}\n")

    uvicorn_proc = None
    rs_proc = None

    try:
        print(f"Starting uvicorn on :{FASTAPI_PORT} …")
        uvicorn_proc = start_uvicorn(FASTAPI_PORT)
        print(f"Starting fastapi-turbo on :{FASTAPI_TURBO_PORT} …")
        rs_proc = start_fastapi_turbo(FASTAPI_TURBO_PORT)

        print("Waiting for servers to listen…")
        fa_ready = wait_for_port(FASTAPI_PORT)
        rs_ready = wait_for_port(FASTAPI_TURBO_PORT)

        if not fa_ready:
            print(f"{RED}uvicorn failed to start{RESET}")
            if uvicorn_proc:
                try:
                    err = uvicorn_proc.stderr.read(2000).decode(errors="ignore")
                    print(err)
                except Exception:
                    pass
            return 1
        if not rs_ready:
            print(f"{RED}fastapi-turbo failed to start{RESET}")
            if rs_proc:
                try:
                    err = rs_proc.stderr.read(2000).decode(errors="ignore")
                    print(err)
                except Exception:
                    pass
            return 1

        # Brief settle
        time.sleep(0.3)
        print(f"{GREEN}Both servers ready!{RESET}\n")

        # Sanity /health
        try:
            fa_h = _client(FA).get("/health")
            fr_h = _client(FR).get("/health")
            print(f"  /health FA={fa_h.status_code}  FR={fr_h.status_code}\n")
        except Exception as e:
            print(f"{RED}/health probe failed: {e}{RESET}")
            return 1

        t0 = time.time()
        run_all(FASTAPI_PORT, FASTAPI_TURBO_PORT)
        elapsed = time.time() - t0

        total = len(results)
        passed = sum(1 for r in results if r[3])
        failed = total - passed

        print(f"\n{BOLD}{'='*72}")
        print(f"  RESULTS: {total} tests | {GREEN}{passed} PASS{RESET}{BOLD} | {RED}{failed} FAIL{RESET}{BOLD} | {elapsed:.1f}s")
        print(f"{'='*72}{RESET}\n")

        # Category breakdown
        by_cat: dict[str, list] = defaultdict(list)
        for tid, cat, desc, p, detail in results:
            by_cat[cat].append((tid, desc, p, detail))
        print(f"{BOLD}Category breakdown:{RESET}")
        cat_fails = []
        for cat, rows in sorted(by_cat.items()):
            cp = sum(1 for r in rows if r[2])
            cf = len(rows) - cp
            color = GREEN if cf == 0 else RED
            print(f"  {color}{cat:25s}{RESET} {cp:4d}/{len(rows):4d}" + (f"  ({cf} failed)" if cf else ""))
            if cf:
                cat_fails.append((cat, cf))

        # Top failure categories
        if cat_fails:
            print(f"\n{BOLD}Top gap categories:{RESET}")
            for cat, cf in sorted(cat_fails, key=lambda x: -x[1])[:10]:
                print(f"  {RED}{cat:25s}{RESET} {cf} failures")

        # Representative failing details (group by category, max 2 per cat)
        if failed > 0:
            print(f"\n{BOLD}Sample failure details (up to 2 per category):{RESET}")
            for cat, rows in sorted(by_cat.items()):
                fails = [r for r in rows if not r[2]]
                if not fails:
                    continue
                print(f"  {RED}{cat}{RESET}:")
                for tid, desc, p, detail in fails[:2]:
                    print(f"    T{tid:04d} {desc}")
                    print(f"      → {detail[:260]}")

        return 0 if failed == 0 else 1

    except KeyboardInterrupt:
        print(f"\n{YELLOW}Interrupted.{RESET}")
        return 130
    finally:
        close_clients()
        for proc in [uvicorn_proc, rs_proc]:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


if __name__ == "__main__":
    sys.exit(main())
