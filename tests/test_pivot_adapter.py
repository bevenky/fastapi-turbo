"""Equivalence test for the pivot adapter (_introspect_from_real_fastapi).

Builds REAL FastAPI apps, drives each route through the adapter → Rust oneshot
door (register_app_router + process_request), and asserts the response matches
real FastAPI's OWN response (httpx.ASGITransport) — the behavioral parity oracle
that gates turning the adapter on. Also asserts the bounded-coverage cases
(yield deps, deps with special params, Form model-expansion) raise Undelegable so
the caller delegates to real FastAPI.

Runs in a subprocess with the compat shim disabled so `import fastapi` is REAL.
"""

from __future__ import annotations

import os
import subprocess
import sys

_SCRIPT = r'''
import asyncio, inspect, json, sys
from typing import Annotated as typing_Annotated
import httpx
import fastapi                       # REAL fastapi (no shim)
from fastapi import (FastAPI, Header, Cookie, Depends, Form, File, UploadFile,
                     Query, Request, Response, BackgroundTasks)
from fastapi.routing import APIRoute
from pydantic import BaseModel
import fastapi_turbo                 # NO_SHIM=1 → no shim installed
from fastapi_turbo._fastapi_turbo_core import RouteInfo, register_app_router, process_request
from fastapi_turbo._introspect_from_real_fastapi import (
    extract_params_from_route, build_handler, Undelegable)

class Item(BaseModel):
    name: str
    qty: int = 1

class Out(BaseModel):
    name: str
    qty: int = 7

class Filters(BaseModel):
    limit: int = 10
    q: str = ""

class HFilters(BaseModel):
    x_tok: str = "none"

def common_dep(q: str = "z"):
    return {"q": q}

def sub_dep(region: str = Header(default="us")):
    return region.upper()

def outer_dep(reg: str = Depends(sub_dep), n: int = 1):
    return {"reg": reg, "n": n}

async def adep(x: int = 0):
    return x * 10

def auth_dep(token: str = Header(default="anon")):
    return token

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
    # --- constraints ---
    @app.get("/con")
    def con(n: int = Query(gt=5, le=10)):
        return {"n": n}
    # --- special params ---
    @app.get("/req")
    def req(request: Request, q: str = "z"):
        return {"method": request.method, "path": request.url.path, "q": q}
    @app.get("/bg")
    def bg(tasks: BackgroundTasks, q: str = "z"):
        tasks.add_task(lambda: None)
        return {"q": q}
    # --- Response inject: temporal header + status_code merge ---
    @app.get("/resp")
    def resp(response: Response, q: str = "z"):
        response.headers["x-custom"] = "yes"
        response.status_code = 201
        return {"q": q}
    # --- special param COMBINED with a dependency (unified-loop path) ---
    @app.get("/reqdep")
    def reqdep(request: Request, who: str = Depends(auth_dep)):
        return {"who": who, "method": request.method}
    # --- form / file ---
    @app.post("/form")
    def form(name: str = Form(), age: int = Form()):
        return {"name": name, "age": age}
    @app.post("/file")
    def file(f: UploadFile = File()):
        return {"filename": f.filename}
    @app.post("/fbytes")
    def fbytes(b: bytes = File()):
        return {"size": len(b)}
    # --- form COMBINED with a dependency ---
    @app.post("/formdep")
    def formdep(name: str = Form(), who: str = Depends(auth_dep)):
        return {"name": name, "who": who}
    # --- response_model ---
    @app.get("/rm", response_model=Out, response_model_exclude_unset=True)
    def rm():
        return {"name": "z"}                            # qty omitted → excluded
    @app.get("/rm2", response_model=Out)
    def rm2():
        return {"name": "z", "extra": "drop"}           # extra key filtered out
    # --- query parameter-model (FastAPI 0.115+) ---
    @app.get("/pm")
    def pm(f: typing_Annotated[Filters, Query()]):
        return {"limit": f.limit, "q": f.q}
    # --- header parameter-model ---
    @app.get("/hpm")
    def hpm(h: typing_Annotated[HFilters, Header()]):
        return {"x_tok": h.x_tok}
    # --- Body(embed=True) ---
    @app.post("/embed")
    def embed(item: Item = fastapi.Body(embed=True)):
        return {"name": item.name, "qty": item.qty}
    # --- multiple JSON bodies ---
    @app.post("/multi")
    def multi(item: Item, out: Out):
        return {"i": item.name, "o": out.name}
    return app

def register(app):
    infos = []
    for r in app.routes:
        if not isinstance(r, APIRoute):
            continue
        params = extract_params_from_route(r)            # may raise Undelegable
        handler = build_handler(r)
        infos.append(RouteInfo(path=r.path, methods=list(r.methods), handler=handler,
                               is_async=inspect.iscoroutinefunction(handler),
                               handler_name=getattr(handler, "__name__", "h"),
                               params=params, is_websocket=False))
    register_app_router(id(app), infos, "127.0.0.1", 0, [], None, None, None, None, [],
                        None, True, None, None, app, None, None, None, None, [])

async def real_response(app, method, path, **kw):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.request(method, path, **kw)
        ct = r.headers.get("content-type", "")
        return r.status_code, (r.json() if ct.startswith("application/json") else r.text)

def door(app, method, path, qs="", headers=None, body=b""):
    h = [(b"host", b"t")] + [(k.encode(), v.encode()) for k, v in (headers or {}).items()]
    st, _hdrs, b = process_request(id(app), method, path, qs, h, body, "127.0.0.1", 50000)
    try:
        return st, json.loads(b)
    except Exception:
        return st, b.decode()

def urlencoded(fields):
    from urllib.parse import urlencode
    return ("application/x-www-form-urlencoded", urlencode(fields).encode())

def multipart(fields=None, files=None, boundary="----turbotestboundary"):
    parts = []
    for k, v in (fields or {}).items():
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"'
                      f'\r\n\r\n{v}\r\n').encode())
    for k, (fn, content) in (files or {}).items():
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; '
                      f'filename="{fn}"\r\nContent-Type: application/octet-stream\r\n\r\n')
                     .encode() + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return (f"multipart/form-data; boundary={boundary}", b"".join(parts))

def main():
    app = build_app()
    register(app)
    fails = []

    # (method, path, qs, headers, body, realkw) — JSON-body / scalar cases.
    cases = [
        ("GET", "/scalars/7", "q=hi&n=3", None, b"", {}),
        ("GET", "/opt", "maybe=5", None, b"", {}),
        ("GET", "/opt", "", None, b"", {}),
        ("GET", "/hdr", "", {"x-tok": "abc"}, b"", {}),
        ("POST", "/body", "", {"content-type": "application/json"},
            json.dumps({"name": "w", "qty": 4}).encode(), {"json": {"name": "w", "qty": 4}}),
        ("POST", "/body", "", {"content-type": "application/json"},
            json.dumps({"name": "w", "qty": "bad"}).encode(), {"json": {"name": "w", "qty": "bad"}}),
        ("GET", "/dep1", "q=hi&top=9", None, b"", {}),
        ("GET", "/dep2", "n=3", {"region": "eu"}, b"", {}),
        ("GET", "/dep3", "x=4", None, b"", {}),
        ("GET", "/con", "n=7", None, b"", {}),                       # constraint pass
        ("GET", "/con", "n=3", None, b"", {}),                       # constraint fail → 422
        ("GET", "/con", "n=99", None, b"", {}),                      # constraint fail → 422
        ("GET", "/req", "q=hey", None, b"", {}),                     # Request inject
        ("GET", "/bg", "q=yo", None, b"", {}),                       # BackgroundTasks inject
        ("GET", "/reqdep", "", {"token": "abc"}, b"", {}),           # Request + dep
        ("GET", "/rm", "", None, b"", {}),                           # response_model exclude_unset
        ("GET", "/rm2", "", None, b"", {}),                          # response_model filter extra
        ("GET", "/pm", "limit=3&q=hi", None, b"", {}),               # query parameter-model
        ("GET", "/pm", "", None, b"", {}),                           # param-model defaults
        ("GET", "/pm", "limit=notint", None, b"", {}),               # param-model bad → 422
        ("GET", "/hpm", "", {"x-tok": "T"}, b"", {}),                # header parameter-model
    ]
    for method, path, qs, hdrs, body, realkw in cases:
        d_st, d_body = door(app, method, path, qs, hdrs, body)
        rurl = path + ("?" + qs if qs else "")
        r_st, r_body = asyncio.run(real_response(app, method, rurl, headers=hdrs, **realkw))
        ok = (d_st == r_st)
        if d_st == 200:
            ok = ok and (d_body == r_body)
        if not ok:
            fails.append(f"{method} {rurl}: door={d_st}/{d_body!r} real={r_st}/{r_body!r}")

    # Response inject: assert the temporal status_code + header merge (door
    # returns raw headers; real FastAPI applies them to the actual response).
    h = [(b"host", b"t")]
    st, hdrs, rb = process_request(id(app), "GET", "/resp", "q=hi", h, b"", "127.0.0.1", 50000)
    hd = {k.decode().lower(): v.decode() for k, v in hdrs}
    if not (st == 201 and hd.get("x-custom") == "yes" and json.loads(rb) == {"q": "hi"}):
        fails.append(f"/resp inject: st={st} hdrs={hd} body={rb!r}")

    # Form / File cases need matching wire bodies for door vs httpx oracle.
    ct, b = urlencoded({"name": "ann", "age": "30"})
    d_st, d_body = door(app, "POST", "/form", "", {"content-type": ct}, b)
    r_st, r_body = asyncio.run(real_response(app, "POST", "/form", data={"name": "ann", "age": "30"}))
    if not (d_st == r_st == 200 and d_body == r_body):
        fails.append(f"/form: door={d_st}/{d_body!r} real={r_st}/{r_body!r}")

    ct, b = multipart(files={"f": ("hello.txt", b"hi there")})
    d_st, d_body = door(app, "POST", "/file", "", {"content-type": ct}, b)
    r_st, r_body = asyncio.run(real_response(app, "POST", "/file", files={"f": ("hello.txt", b"hi there")}))
    if not (d_st == r_st == 200 and d_body == r_body):
        fails.append(f"/file: door={d_st}/{d_body!r} real={r_st}/{r_body!r}")

    ct, b = multipart(files={"b": ("x.bin", b"abcdef")})
    d_st, d_body = door(app, "POST", "/fbytes", "", {"content-type": ct}, b)
    r_st, r_body = asyncio.run(real_response(app, "POST", "/fbytes", files={"b": ("x.bin", b"abcdef")}))
    if not (d_st == r_st == 200 and d_body == r_body):
        fails.append(f"/fbytes: door={d_st}/{d_body!r} real={r_st}/{r_body!r}")

    ct, b = multipart(fields={"name": "ann"})
    d_st, d_body = door(app, "POST", "/formdep", "", {"content-type": ct, "token": "abc"}, b)
    r_st, r_body = asyncio.run(real_response(app, "POST", "/formdep", data={"name": "ann"},
                                             headers={"token": "abc"}))
    if not (d_st == r_st == 200 and d_body == r_body):
        fails.append(f"/formdep: door={d_st}/{d_body!r} real={r_st}/{r_body!r}")

    # Body(embed=True): {"item": {...}}
    eb = json.dumps({"item": {"name": "w", "qty": 4}}).encode()
    d_st, d_body = door(app, "POST", "/embed", "", {"content-type": "application/json"}, eb)
    r_st, r_body = asyncio.run(real_response(app, "POST", "/embed",
                                             json={"item": {"name": "w", "qty": 4}}))
    if not (d_st == r_st == 200 and d_body == r_body):
        fails.append(f"/embed: door={d_st}/{d_body!r} real={r_st}/{r_body!r}")

    # Multiple JSON bodies: {"item": {...}, "out": {...}}
    mb = json.dumps({"item": {"name": "i"}, "out": {"name": "o"}}).encode()
    d_st, d_body = door(app, "POST", "/multi", "", {"content-type": "application/json"}, mb)
    r_st, r_body = asyncio.run(real_response(app, "POST", "/multi",
                                             json={"item": {"name": "i"}, "out": {"name": "o"}}))
    if not (d_st == r_st == 200 and d_body == r_body):
        fails.append(f"/multi: door={d_st}/{d_body!r} real={r_st}/{r_body!r}")

    # decline coverage: yield-dep, dep-with-special-param, Form model-expansion.
    dec_app = FastAPI()
    def ydep():
        yield "x"
    def reqdep_inner(request: Request):                  # dep takes Request → decline
        return request.method
    class FModel(BaseModel):
        a: int
    def plain(q: str = "z"):
        return q
    @dec_app.get("/y")
    def y(v: str = Depends(ydep)): return {"v": v}
    @dec_app.get("/rd")
    def rd(m: str = Depends(reqdep_inner)): return {"m": m}
    @dec_app.post("/fm")
    def fm(model: FModel = Form()): return {"a": model.a}
    @dec_app.get("/ok")
    def okr(v: str = Depends(plain)): return {"v": v}
    for r in dec_app.routes:
        if not isinstance(r, APIRoute):
            continue
        if r.path in ("/y", "/rd", "/fm"):
            try:
                extract_params_from_route(r); fails.append(f"{r.path} should be Undelegable")
            except Undelegable:
                pass
        elif r.path == "/ok":
            try:
                extract_params_from_route(r)
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
