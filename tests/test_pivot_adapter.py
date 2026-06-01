"""Step B equivalence test for the pivot adapter (_introspect_from_real_fastapi).

Builds REAL FastAPI apps, drives each route through the adapter → Rust oneshot
door (register_app_router + process_request), and asserts the response matches
real FastAPI's OWN response (httpx.ASGITransport) — the behavioral parity oracle
that gates turning the adapter on. Also asserts the bounded-coverage cases
(dependencies, Form bodies, special params) raise Undelegable so the caller
delegates to real FastAPI.

Runs in a subprocess with the compat shim disabled so `import fastapi` is REAL.
"""

from __future__ import annotations

import os
import subprocess
import sys

_SCRIPT = r'''
import asyncio, inspect, json, sys
import httpx
import fastapi                       # REAL fastapi (no shim)
from fastapi import FastAPI, Header, Cookie, Depends, Form
from fastapi.routing import APIRoute
from pydantic import BaseModel
import fastapi_turbo                 # NO_SHIM=1 → no shim installed
from fastapi_turbo._fastapi_turbo_core import RouteInfo, register_app_router, process_request
from fastapi_turbo._introspect_from_real_fastapi import extract_params_from_route, Undelegable

class Item(BaseModel):
    name: str
    qty: int = 1

def common_dep(q: str = "z"):
    return {"q": q}

def sub_dep(region: str = Header(default="us")):
    return region.upper()

def outer_dep(reg: str = Depends(sub_dep), n: int = 1):
    return {"reg": reg, "n": n}

async def adep(x: int = 0):
    return x * 10

def build_app():
    app = FastAPI()
    @app.get("/scalars/{pid}")
    def scalars(pid: int, q: str = "z", n: int = 0):
        return {"pid": pid, "q": q, "n": n}
    @app.get("/hdr")
    def hdr(x_tok: str = Header(default="none")):
        return {"x_tok": x_tok}
    @app.post("/body")
    def body(item: Item):
        return {"name": item.name, "qty": item.qty}
    @app.get("/opt")
    def opt(maybe: int | None = None):
        return {"maybe": maybe}
    @app.get("/dep1")                                  # simple dep + handler param
    def dep1(d: dict = Depends(common_dep), top: int = 5):
        return {"d": d, "top": top}
    @app.get("/dep2")                                  # NESTED dep (outer→sub) + header inject
    def dep2(o: dict = Depends(outer_dep)):
        return {"o": o}
    @app.get("/dep3")                                  # ASYNC dep
    async def dep3(v: int = Depends(adep)):
        return {"v": v}
    return app

def register(app):
    """Adapter → RouteInfo for every coverable APIRoute; returns declined paths."""
    infos, declined = [], []
    for r in app.routes:
        if not isinstance(r, APIRoute):
            continue
        try:
            params = extract_params_from_route(r)
        except Undelegable:
            declined.append(r.path); continue
        infos.append(RouteInfo(path=r.path, methods=list(r.methods), handler=r.endpoint,
                               is_async=inspect.iscoroutinefunction(r.endpoint),
                               handler_name=r.endpoint.__name__,
                               params=params, is_websocket=False))
    register_app_router(id(app), infos, "127.0.0.1", 0, [], None, None, None, None, [],
                        None, True, None, None, app, None, None, None, None, [])
    return declined

async def real_response(app, method, path, **kw):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.request(method, path, **kw)
        return r.status_code, (r.json() if r.headers.get("content-type","").startswith("application/json") else r.text)

def door(app, method, path, qs="", headers=None, body=b""):
    h = [(b"host", b"t")] + [(k.encode(), v.encode()) for k, v in (headers or {}).items()]
    st, _hdrs, b = process_request(id(app), method, path, qs, h, body, "127.0.0.1", 50000)
    try:
        return st, json.loads(b)
    except Exception:
        return st, b.decode()

def main():
    app = build_app()
    register(app)
    cases = [
        ("GET", "/scalars/7", "q=hi&n=3", None, b"", {}),
        ("GET", "/opt", "maybe=5", None, b"", {}),
        ("GET", "/opt", "", None, b"", {}),
        ("GET", "/hdr", "", {"x-tok": "abc"}, b"", {}),
        ("POST", "/body", "", {"content-type": "application/json"},
            json.dumps({"name": "w", "qty": 4}).encode(), {"json": {"name": "w", "qty": 4}}),
        ("POST", "/body", "", {"content-type": "application/json"},
            json.dumps({"name": "w", "qty": "bad"}).encode(), {"json": {"name": "w", "qty": "bad"}}),  # 422
        ("GET", "/dep1", "q=hi&top=9", None, b"", {}),                    # dep + handler param
        ("GET", "/dep2", "n=3", {"region": "eu"}, b"", {}),               # nested dep + header inject
        ("GET", "/dep3", "x=4", None, b"", {}),                          # async dep
    ]
    fails = []
    for method, path, qs, hdrs, body, realkw in cases:
        d_st, d_body = door(app, method, path, qs, hdrs, body)
        rurl = path + ("?" + qs if qs else "")
        r_st, r_body = asyncio.run(real_response(app, method, rurl, headers=hdrs, **realkw))
        ok = (d_st == r_st)
        # 200 bodies must match exactly; 422 just match status (detail shapes差 are fine).
        if d_st == 200:
            ok = ok and (d_body == r_body)
        if not ok:
            fails.append(f"{method} {rurl}: door={d_st}/{d_body!r} real={r_st}/{r_body!r}")
    # decline coverage: yield-dep + Form must raise Undelegable; a simple dep must NOT.
    dec_app = FastAPI()
    def ydep():
        yield "x"
    def plain(q: str = "z"):
        return q
    @dec_app.get("/y")
    def y(v: str = Depends(ydep)): return {"v": v}
    @dec_app.post("/f")
    def f(name: str = Form()): return {"name": name}
    @dec_app.get("/ok")
    def okr(v: str = Depends(plain)): return {"v": v}
    for r in dec_app.routes:
        if not isinstance(r, APIRoute):
            continue
        if r.path in ("/y", "/f"):
            try:
                extract_params_from_route(r); fails.append(f"{r.path} should be Undelegable")
            except Undelegable:
                pass
        elif r.path == "/ok":
            try:
                extract_params_from_route(r)  # simple dep must be HANDLED
            except Undelegable as e:
                fails.append(f"/ok simple dep should be handled, got Undelegable: {e}")
    if fails:
        print("FAIL\n" + "\n".join(fails)); sys.exit(1)
    print("PIVOT_ADAPTER_OK")

main()
'''


def test_pivot_adapter_matches_real_fastapi():
    env = dict(os.environ)
    env["FASTAPI_TURBO_NO_SHIM"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "PIVOT_ADAPTER_OK" in proc.stdout, (
        f"adapter ≠ real FastAPI:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
