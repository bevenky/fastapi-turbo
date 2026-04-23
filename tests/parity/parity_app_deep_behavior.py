"""Deep behavior parity app: instrumented endpoints probing runtime semantics.

This app exercises HOW requests are processed — middleware ordering, dep caching,
streaming boundaries, cookie attributes, exception propagation, request/response
introspection, concurrency, lifespan — not just "does it return 200".

Uses ONLY stock FastAPI imports. The compat shim maps these to fastapi-turbo when
running under fastapi-turbo.
"""
import asyncio
import json
import os
import time
import threading
from contextlib import asynccontextmanager
from typing import Annotated, Optional

from fastapi import (
    FastAPI, APIRouter, Depends, Query, Path, Header, Cookie, Body, Form,
    HTTPException, Request, Response, BackgroundTasks,
)
from fastapi.responses import (
    JSONResponse, HTMLResponse, PlainTextResponse, RedirectResponse,
    StreamingResponse,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel


# ═══════════════════════════════════════════════════════════════════
# Global counters — instrumentation for call tracking
# ═══════════════════════════════════════════════════════════════════

# Each dep tracks total calls (not per-request)
_call_counts = {}

def bump(key):
    _call_counts[key] = _call_counts.get(key, 0) + 1
    return _call_counts[key]

def get_count(key):
    return _call_counts.get(key, 0)

def reset_counts():
    _call_counts.clear()


# ═══════════════════════════════════════════════════════════════════
# Lifespan
# ═══════════════════════════════════════════════════════════════════

_lifespan_events = []

@asynccontextmanager
async def lifespan(app: FastAPI):
    _lifespan_events.append("startup")
    app.state.db = {"counter": 0}
    app.state.name = "deep_behavior_app"
    app.state.started_at = time.time()
    yield
    _lifespan_events.append("shutdown")


# ═══════════════════════════════════════════════════════════════════
# App
# ═══════════════════════════════════════════════════════════════════

app = FastAPI(
    title="Deep Behavior Parity",
    version="1.0.0",
    lifespan=lifespan,
)


# ═══════════════════════════════════════════════════════════════════
# Middleware (order matters — registered outermost-last)
# ═══════════════════════════════════════════════════════════════════

# NB: In Starlette/FastAPI, @app.middleware('http') decorators are applied in
# reverse registration order (last registered runs first on the request path).

# mw_a is registered FIRST. In Starlette, decorated middlewares are applied so
# that the first-registered is INNERMOST. Request path: C→B→A→handler. Response
# path: A→B→C. The OUTERMOST (C) therefore sees the full order log on the way out.

@app.middleware("http")
async def mw_a(request: Request, call_next):
    request.state.order_log = getattr(request.state, "order_log", []) + ["A_in"]
    resp = await call_next(request)
    request.state.order_log.append("A_out")
    resp.headers["X-MW-A"] = "seen"
    return resp


@app.middleware("http")
async def mw_b(request: Request, call_next):
    request.state.order_log = getattr(request.state, "order_log", []) + ["B_in"]
    resp = await call_next(request)
    request.state.order_log.append("B_out")
    resp.headers["X-MW-B"] = "seen"
    return resp


# Outermost: runs first on request and last on response — serializes the full log.
@app.middleware("http")
async def mw_c(request: Request, call_next):
    request.state.order_log = getattr(request.state, "order_log", []) + ["C_in"]
    if request.headers.get("X-Short-Circuit") == "yes":
        return JSONResponse({"short_circuited": True}, status_code=299)
    resp = await call_next(request)
    request.state.order_log.append("C_out")
    resp.headers["X-MW-C"] = "seen"
    resp.headers["X-Call-Order"] = ",".join(request.state.order_log)
    return resp


app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=1000)


# ═══════════════════════════════════════════════════════════════════
# Dependencies
# ═══════════════════════════════════════════════════════════════════

def dep_simple():
    bump("dep_simple")
    return "simple"


def dep_cached():
    n = bump("dep_cached")
    return {"call": n}


def dep_uncached():
    n = bump("dep_uncached")
    return {"call": n}


def dep_inner():
    bump("dep_inner")
    return "inner"


def dep_outer(inner=Depends(dep_inner)):
    bump("dep_outer")
    return f"outer[{inner}]"


# Shared inner dep used by 2 outer deps within one handler (for cache test)
def dep_shared():
    bump("dep_shared")
    return "shared"


def dep_first(s=Depends(dep_shared)):
    bump("dep_first")
    return f"first[{s}]"


def dep_second(s=Depends(dep_shared)):
    bump("dep_second")
    return f"second[{s}]"


# Yield deps
_yield_events = []

def dep_yield_a():
    _yield_events.append("yield_a_setup")
    try:
        yield "A"
    finally:
        _yield_events.append("yield_a_teardown")


def dep_yield_b():
    _yield_events.append("yield_b_setup")
    try:
        yield "B"
    finally:
        _yield_events.append("yield_b_teardown")


def dep_yield_c():
    _yield_events.append("yield_c_setup")
    try:
        yield "C"
    finally:
        _yield_events.append("yield_c_teardown")


def dep_yield_exc():
    _yield_events.append("yield_exc_setup")
    try:
        yield "EXC"
    except Exception:
        _yield_events.append("yield_exc_saw_exception")
        raise
    finally:
        _yield_events.append("yield_exc_teardown")


# Background task sink
_bg_log = []

def bg_write(msg):
    _bg_log.append(msg)


# ═══════════════════════════════════════════════════════════════════
# Models
# ═══════════════════════════════════════════════════════════════════

class Item(BaseModel):
    name: str
    price: float


# ═══════════════════════════════════════════════════════════════════
# Exception handlers
# ═══════════════════════════════════════════════════════════════════

class MyCustomError(Exception):
    def __init__(self, message: str):
        self.message = message


@app.exception_handler(MyCustomError)
async def my_custom_handler(request: Request, exc: MyCustomError):
    return JSONResponse({"custom_error": exc.message}, status_code=418)


# ═══════════════════════════════════════════════════════════════════
# Health
# ═══════════════════════════════════════════════════════════════════

@app.get("/health")
def health():
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════════════
# Middleware ordering endpoints
# ═══════════════════════════════════════════════════════════════════

@app.get("/mw/order")
def mw_order(request: Request):
    return {"order_so_far": list(request.state.order_log)}


@app.get("/mw/short-circuit-check")
def mw_sc_check():
    return {"handler_ran": True}


@app.get("/mw/transform-header")
def mw_transform(request: Request):
    # returns headers as seen by handler
    return {
        "has_x_added": request.headers.get("X-Added-By-MW"),
    }


@app.middleware("http")
async def mw_adds_header(request: Request, call_next):
    # Re-build scope for downstream — note: setting headers on scope is non-trivial,
    # but we can modify request.scope directly in Starlette-compatible way.
    # For parity testing we just record.
    resp = await call_next(request)
    resp.headers["X-Final-Added"] = "yes"
    return resp


# ═══════════════════════════════════════════════════════════════════
# Dependency caching
# ═══════════════════════════════════════════════════════════════════

@app.get("/dep/cache/same")
def dep_cache_same(a=Depends(dep_cached), b=Depends(dep_cached), c=Depends(dep_cached)):
    return {"a": a, "b": b, "c": c}


@app.get("/dep/cache/none")
def dep_cache_none(
    a=Depends(dep_uncached, use_cache=False),
    b=Depends(dep_uncached, use_cache=False),
    c=Depends(dep_uncached, use_cache=False),
):
    return {"a": a, "b": b, "c": c}


@app.get("/dep/cache/nested")
def dep_cache_nested(f=Depends(dep_first), s=Depends(dep_second)):
    # dep_shared should be called ONCE
    return {"first": f, "second": s, "shared_calls": get_count("dep_shared")}


@app.get("/dep/cache/reset")
def dep_cache_reset():
    reset_counts()
    _yield_events.clear()
    return {"reset": True}


@app.get("/dep/cache/counts")
def dep_cache_counts():
    return dict(_call_counts)


@app.get("/dep/simple")
def dep_simple_use(s=Depends(dep_simple)):
    return {"value": s}


@app.get("/dep/chained")
def dep_chained_use(o=Depends(dep_outer)):
    return {"value": o}


# ═══════════════════════════════════════════════════════════════════
# Yield deps
# ═══════════════════════════════════════════════════════════════════

@app.get("/yield/order")
def yield_order(a=Depends(dep_yield_a), b=Depends(dep_yield_b), c=Depends(dep_yield_c)):
    # Before teardown fires, yield_events has A_setup, B_setup, C_setup
    return {"a": a, "b": b, "c": c, "events_in_handler": list(_yield_events)}


@app.get("/yield/events")
def yield_events_view():
    return {"events": list(_yield_events)}


@app.get("/yield/clear")
def yield_clear():
    _yield_events.clear()
    return {"cleared": True}


@app.get("/yield/raise-in-handler")
def yield_raise(a=Depends(dep_yield_a)):
    raise HTTPException(status_code=500, detail="boom")


@app.get("/yield/exc-aware")
def yield_exc_aware(e=Depends(dep_yield_exc)):
    raise HTTPException(status_code=500, detail="explode")


# ═══════════════════════════════════════════════════════════════════
# Background tasks
# ═══════════════════════════════════════════════════════════════════

@app.get("/bg/add-one")
def bg_add_one(tasks: BackgroundTasks):
    tasks.add_task(bg_write, "task1")
    return {"scheduled": True}


@app.get("/bg/log")
def bg_log_view():
    return {"log": list(_bg_log)}


@app.get("/bg/clear")
def bg_clear():
    _bg_log.clear()
    return {"cleared": True}


# ═══════════════════════════════════════════════════════════════════
# Request introspection
# ═══════════════════════════════════════════════════════════════════

@app.get("/req/url")
def req_url(request: Request):
    return {
        "path": request.url.path,
        "query": str(request.url.query or ""),
        "scheme": request.url.scheme,
        "port": request.url.port,
    }


@app.get("/req/method")
def req_method(request: Request):
    return {"method": request.method}


@app.api_route("/req/method-multi", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def req_method_multi(request: Request):
    return {"method": request.method}


@app.get("/req/headers-ci")
def req_headers_ci(request: Request):
    return {
        "lower": request.headers.get("x-custom"),
        "upper": request.headers.get("X-Custom"),
        "mixed": request.headers.get("X-cUsToM"),
    }


@app.get("/req/cookies")
def req_cookies(request: Request):
    return {"cookies": dict(request.cookies)}


@app.get("/req/query-params-multi")
def req_qp_multi(request: Request):
    return {
        "all": list(request.query_params.multi_items())
            if hasattr(request.query_params, "multi_items")
            else list(request.query_params.items()),
        "getlist_x": request.query_params.getlist("x")
            if hasattr(request.query_params, "getlist")
            else [request.query_params.get("x", "")],
    }


@app.get("/req/client")
def req_client(request: Request):
    client = request.client
    return {
        "has_host": client is not None and client.host is not None,
        "has_port": client is not None and client.port is not None,
    }


@app.get("/req/scope-type")
def req_scope_type(request: Request):
    return {"type": request.scope.get("type")}


@app.post("/req/raw-body")
async def req_raw_body(request: Request):
    body = await request.body()
    return {"length": len(body), "text": body.decode("utf-8", errors="replace")}


@app.post("/req/json")
async def req_json(request: Request):
    data = await request.json()
    return {"parsed": data}


@app.post("/req/form")
async def req_form(request: Request):
    form = await request.form()
    return {"items": sorted(list(form.items()))}


@app.get("/req/state-in-handler")
def req_state_handler(request: Request):
    request.state.answer = 42
    return {"answer": request.state.answer}


@app.get("/req/app-state")
def req_app_state(request: Request):
    return {"name": request.app.state.name}


# ═══════════════════════════════════════════════════════════════════
# Cookies
# ═══════════════════════════════════════════════════════════════════

@app.get("/cookie/set-basic")
def cookie_set_basic(response: Response):
    response.set_cookie(key="foo", value="bar")
    return {"ok": True}


@app.get("/cookie/set-max-age")
def cookie_set_max_age(response: Response):
    response.set_cookie(key="sess", value="xxx", max_age=3600)
    return {"ok": True}


@app.get("/cookie/set-path")
def cookie_set_path(response: Response):
    response.set_cookie(key="c", value="v", path="/api")
    return {"ok": True}


@app.get("/cookie/set-domain")
def cookie_set_domain(response: Response):
    response.set_cookie(key="c", value="v", domain="example.com")
    return {"ok": True}


@app.get("/cookie/set-secure")
def cookie_set_secure(response: Response):
    response.set_cookie(key="c", value="v", secure=True)
    return {"ok": True}


@app.get("/cookie/set-httponly")
def cookie_set_httponly(response: Response):
    response.set_cookie(key="c", value="v", httponly=True)
    return {"ok": True}


@app.get("/cookie/set-samesite-lax")
def cookie_set_samesite_lax(response: Response):
    response.set_cookie(key="c", value="v", samesite="lax")
    return {"ok": True}


@app.get("/cookie/set-samesite-strict")
def cookie_set_samesite_strict(response: Response):
    response.set_cookie(key="c", value="v", samesite="strict")
    return {"ok": True}


@app.get("/cookie/set-samesite-none")
def cookie_set_samesite_none(response: Response):
    response.set_cookie(key="c", value="v", samesite="none", secure=True)
    return {"ok": True}


@app.get("/cookie/set-multi")
def cookie_set_multi(response: Response):
    response.set_cookie(key="a", value="1")
    response.set_cookie(key="b", value="2")
    response.set_cookie(key="c", value="3")
    return {"ok": True}


@app.get("/cookie/delete")
def cookie_delete(response: Response):
    response.delete_cookie(key="stale")
    return {"ok": True}


@app.get("/cookie/get-one")
def cookie_get_one(foo: str = Cookie(default="NONE")):
    return {"foo": foo}


@app.get("/cookie/get-missing")
def cookie_get_missing(missing: str = Cookie(default="default_missing")):
    return {"missing": missing}


# ═══════════════════════════════════════════════════════════════════
# Response types
# ═══════════════════════════════════════════════════════════════════

@app.get("/resp/dict")
def resp_dict():
    return {"a": 1, "b": 2}


@app.get("/resp/string")
def resp_string():
    return "hello"


@app.get("/resp/int")
def resp_int():
    return 42


@app.get("/resp/bool-true")
def resp_bool_true():
    return True


@app.get("/resp/bool-false")
def resp_bool_false():
    return False


@app.get("/resp/none")
def resp_none():
    return None


@app.get("/resp/list")
def resp_list():
    return [1, 2, 3]


@app.get("/resp/float")
def resp_float():
    return 3.14


@app.get("/resp/explicit-json")
def resp_explicit_json():
    return JSONResponse({"explicit": True})


@app.get("/resp/explicit-status")
def resp_explicit_status():
    return JSONResponse({"x": 1}, status_code=201)


@app.get("/resp/html")
def resp_html():
    return HTMLResponse("<p>hi</p>")


@app.get("/resp/plain")
def resp_plain():
    return PlainTextResponse("plain")


@app.get("/resp/redirect")
def resp_redirect():
    return RedirectResponse(url="/health", status_code=302)


@app.get("/resp/append-header")
def resp_append_header():
    r = Response(content="x")
    r.headers.append("X-Multi", "one")
    r.headers.append("X-Multi", "two")
    return r


@app.get("/resp/override-status")
def resp_override_status(response: Response):
    response.status_code = 201
    return {"ok": True}


@app.get("/resp/custom-header")
def resp_custom_header(response: Response):
    response.headers["X-Handler-Added"] = "yes"
    return {"ok": True}


@app.get("/resp/raw")
def resp_raw():
    return Response(content=b"raw-bytes", media_type="application/octet-stream")


@app.get("/resp/media-type")
def resp_media_type():
    return Response(content="<xml/>", media_type="application/xml")


# ═══════════════════════════════════════════════════════════════════
# Streaming
# ═══════════════════════════════════════════════════════════════════

@app.get("/stream/100-chunks")
def stream_100():
    def gen():
        for i in range(100):
            yield f"chunk-{i:03d}\n"
    return StreamingResponse(gen(), media_type="text/plain")


@app.get("/stream/5-chunks")
def stream_5():
    def gen():
        for i in range(5):
            yield f"c{i}|"
    return StreamingResponse(gen(), media_type="text/plain")


@app.get("/stream/sse")
def stream_sse():
    def gen():
        for i in range(3):
            yield f"data: event-{i}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/stream/async")
async def stream_async():
    async def gen():
        for i in range(10):
            await asyncio.sleep(0.001)
            yield f"async-{i}\n"
    return StreamingResponse(gen(), media_type="text/plain")


@app.get("/stream/bytes")
def stream_bytes():
    def gen():
        for i in range(5):
            yield b"\x00\x01\x02\x03"
    return StreamingResponse(gen(), media_type="application/octet-stream")


@app.get("/stream/empty")
def stream_empty():
    def gen():
        if False:
            yield ""
    return StreamingResponse(gen(), media_type="text/plain")


@app.get("/stream/single")
def stream_single():
    def gen():
        yield "onlychunk"
    return StreamingResponse(gen(), media_type="text/plain")


# ═══════════════════════════════════════════════════════════════════
# Exceptions
# ═══════════════════════════════════════════════════════════════════

@app.get("/exc/http-404")
def exc_http_404():
    raise HTTPException(status_code=404, detail="not found")


@app.get("/exc/http-403")
def exc_http_403():
    raise HTTPException(status_code=403, detail="forbidden")


@app.get("/exc/http-headers")
def exc_http_headers():
    raise HTTPException(status_code=401, detail="unauth", headers={"WWW-Authenticate": "Bearer"})


@app.get("/exc/value-error")
def exc_value_error():
    raise ValueError("nope")


@app.get("/exc/type-error")
def exc_type_error():
    raise TypeError("nope2")


@app.get("/exc/custom")
def exc_custom():
    raise MyCustomError("custom!")


@app.post("/exc/validation")
def exc_validation(body: Item):
    return body.model_dump()


@app.get("/exc/validation-query")
def exc_validation_query(n: int):
    return {"n": n}


@app.get("/exc/http-detail-dict")
def exc_http_detail_dict():
    raise HTTPException(status_code=400, detail={"code": "E_BAD", "reason": "malformed"})


# ═══════════════════════════════════════════════════════════════════
# State / lifespan
# ═══════════════════════════════════════════════════════════════════

@app.get("/state/name")
def state_name(request: Request):
    return {"name": request.app.state.name}


@app.get("/state/db")
def state_db(request: Request):
    return {"db": dict(request.app.state.db)}


@app.get("/state/incr")
def state_incr(request: Request):
    request.app.state.db["counter"] += 1
    return {"counter": request.app.state.db["counter"]}


# ═══════════════════════════════════════════════════════════════════
# Concurrency test endpoint
# ═══════════════════════════════════════════════════════════════════

@app.get("/concurrent/echo/{n}")
def concurrent_echo(n: int):
    return {"n": n, "doubled": n * 2}


@app.get("/concurrent/slow")
async def concurrent_slow():
    await asyncio.sleep(0.02)
    return {"slow": True}


@app.get("/concurrent/fast")
def concurrent_fast():
    return {"fast": True}


@app.get("/concurrent/scope-leak")
def concurrent_scope_leak(request: Request):
    # Each request should have its own state
    req_id = request.headers.get("X-Req-Id", "")
    request.state.req_id = req_id
    # Busy loop a tiny bit to allow interleaving
    total = 0
    for i in range(1000):
        total += i
    return {"my_req_id": request.state.req_id}


# ═══════════════════════════════════════════════════════════════════
# Path / Query / Header behaviors
# ═══════════════════════════════════════════════════════════════════

@app.get("/pp/int/{x}")
def pp_int(x: int):
    return {"x": x, "type": "int"}


@app.get("/pp/str/{x}")
def pp_str(x: str):
    return {"x": x, "type": "str"}


@app.get("/pp/float/{x}")
def pp_float(x: float):
    return {"x": x, "type": "float"}


@app.get("/pp/bool/{x}")
def pp_bool(x: bool):
    return {"x": x, "type": "bool"}


@app.get("/pp/path/{p:path}")
def pp_path(p: str):
    return {"p": p}


@app.get("/pp/default-query")
def pp_default_query(q: str = "default_q"):
    return {"q": q}


@app.get("/pp/required-query")
def pp_required_query(q: str):
    return {"q": q}


@app.get("/pp/list-query")
def pp_list_query(tag: list[str] = Query(default=[])):
    return {"tags": tag}


@app.get("/pp/alias-query")
def pp_alias_query(my_val: str = Query(default="d", alias="myVal")):
    return {"v": my_val}


@app.get("/pp/header-underscore")
def pp_header_underscore(x_custom: str = Header(default="none")):
    return {"x": x_custom}


@app.get("/pp/header-alias")
def pp_header_alias(custom: str = Header(default="none", alias="X-Custom-Alias")):
    return {"x": custom}


@app.get("/pp/numeric-constraints")
def pp_numeric(age: int = Query(ge=0, le=150)):
    return {"age": age}


# ═══════════════════════════════════════════════════════════════════
# HEAD / OPTIONS / trailing slash
# ═══════════════════════════════════════════════════════════════════

@app.get("/method/get")
def method_get():
    return {"m": "GET"}


@app.post("/method/post")
def method_post():
    return {"m": "POST"}


@app.put("/method/put")
def method_put():
    return {"m": "PUT"}


@app.delete("/method/delete")
def method_delete():
    return {"m": "DELETE"}


@app.patch("/method/patch")
def method_patch():
    return {"m": "PATCH"}


# ═══════════════════════════════════════════════════════════════════
# CORS preflight check
# ═══════════════════════════════════════════════════════════════════

@app.get("/cors/endpoint")
def cors_endpoint():
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════
# GZip threshold
# ═══════════════════════════════════════════════════════════════════

@app.get("/gzip/big")
def gzip_big():
    # 2KB → above default 1KB threshold
    return {"payload": "x" * 2000}


@app.get("/gzip/small")
def gzip_small():
    return {"payload": "tiny"}


# ═══════════════════════════════════════════════════════════════════
# Routers with dependencies
# ═══════════════════════════════════════════════════════════════════

_router_seen = []

def router_dep_fn():
    _router_seen.append("router_dep")
    return "router_dep"


sub_router = APIRouter(prefix="/sub", dependencies=[Depends(router_dep_fn)])


@sub_router.get("/a")
def sub_a():
    return {"r": "a"}


@sub_router.get("/b")
def sub_b():
    return {"r": "b"}


app.include_router(sub_router)


@app.get("/router/seen")
def router_seen():
    return {"seen": list(_router_seen)}


@app.get("/router/clear")
def router_clear():
    _router_seen.clear()
    return {"cleared": True}


# ═══════════════════════════════════════════════════════════════════
# Misc behavior
# ═══════════════════════════════════════════════════════════════════

@app.get("/misc/header-dupe")
def misc_header_dupe():
    r = JSONResponse({"ok": True})
    r.headers.append("X-Dupe", "one")
    r.headers.append("X-Dupe", "two")
    return r


@app.get("/misc/empty-list")
def misc_empty_list():
    return []


@app.get("/misc/empty-dict")
def misc_empty_dict():
    return {}


@app.get("/misc/unicode")
def misc_unicode():
    return {"emoji": "hello world", "chinese": "你好"}


@app.get("/misc/large-response")
def misc_large():
    return {"data": list(range(500))}


@app.get("/misc/content-length")
def misc_content_length():
    return {"x": "y"}


@app.post("/misc/echo-json")
async def misc_echo_json(request: Request):
    data = await request.json()
    return {"received": data}


@app.post("/misc/content-type")
async def misc_ct(request: Request):
    return {"ct": request.headers.get("content-type", "")}


# ═══════════════════════════════════════════════════════════════════
# Response model
# ═══════════════════════════════════════════════════════════════════

class ResponseModel(BaseModel):
    name: str
    price: float


@app.get("/rm/strip", response_model=ResponseModel)
def rm_strip():
    return {"name": "a", "price": 1.0, "secret": "hidden"}


@app.get("/rm/pydantic", response_model=ResponseModel)
def rm_pydantic():
    return ResponseModel(name="b", price=2.0)


@app.post("/rm/echo", response_model=Item)
def rm_echo(item: Item):
    return item


# ═══════════════════════════════════════════════════════════════════
# Empty body handlers
# ═══════════════════════════════════════════════════════════════════

@app.post("/empty/accept")
async def empty_accept(request: Request):
    body = await request.body()
    return {"len": len(body)}


# ═══════════════════════════════════════════════════════════════════
# JSON serialization edge cases
# ═══════════════════════════════════════════════════════════════════

@app.get("/json/nested-deep")
def json_nested_deep():
    d = {"v": 0}
    for _ in range(20):
        d = {"nested": d}
    return d


@app.get("/json/list-of-dicts")
def json_list_of_dicts():
    return [{"i": i, "sq": i * i} for i in range(20)]


@app.get("/json/mixed-types")
def json_mixed():
    return {
        "int": 42,
        "float": 3.14,
        "bool_t": True,
        "bool_f": False,
        "null": None,
        "str": "hi",
        "list": [1, 2, 3],
        "dict": {"a": 1},
    }


@app.get("/json/large-number")
def json_large_number():
    return {"n": 2**31 - 1, "big": 2**53 - 1}


@app.get("/json/neg-number")
def json_neg_number():
    return {"n": -42, "f": -3.14}


@app.get("/json/list-nulls")
def json_list_nulls():
    return [1, None, "two", None, 4]


@app.get("/json/dict-with-null")
def json_dict_null():
    return {"a": None, "b": 2}


@app.get("/json/empty-string")
def json_empty_str():
    return {"s": ""}


@app.get("/json/special-chars")
def json_special_chars():
    return {"s": 'with "quote" and \n newline and \t tab'}


# ═══════════════════════════════════════════════════════════════════
# Header edge cases
# ═══════════════════════════════════════════════════════════════════

@app.get("/hdr/get-all")
def hdr_get_all(request: Request):
    return {
        "ua": request.headers.get("user-agent", ""),
        "host": request.headers.get("host", ""),
        "has_accept": "accept" in request.headers,
    }


@app.get("/hdr/multi-value")
def hdr_multi(request: Request):
    # getlist / items equivalent
    return {"x_list": request.headers.get("x-list", "")}


@app.get("/hdr/accept-encoding")
def hdr_accept_encoding(accept_encoding: str = Header(default="")):
    return {"ae": accept_encoding}


@app.get("/hdr/authorization")
def hdr_auth(authorization: str = Header(default="")):
    return {"authz": authorization}


# ═══════════════════════════════════════════════════════════════════
# Dependency edge cases
# ═══════════════════════════════════════════════════════════════════

async def dep_async():
    bump("dep_async")
    return "async_val"


async def dep_async_with_sleep():
    await asyncio.sleep(0.001)
    bump("dep_async_sleep")
    return "async_slept"


class DepClass:
    def __init__(self):
        bump("depclass_init")

    def __call__(self):
        bump("depclass_call")
        return "class_inst"


dep_class_instance = DepClass()


def dep_class_callable():
    bump("dep_callable")
    return "callable"


@app.get("/dep/async")
async def use_dep_async(v=Depends(dep_async)):
    return {"v": v}


@app.get("/dep/async-sleep")
async def use_dep_async_sleep(v=Depends(dep_async_with_sleep)):
    return {"v": v}


@app.get("/dep/class-instance")
def use_dep_class_instance(v=Depends(dep_class_instance)):
    return {"v": v}


@app.get("/dep/class")
def use_dep_class(v=Depends(DepClass)):
    return {"v": v}


# Dep that raises
def dep_raises():
    raise HTTPException(status_code=401, detail="dep_unauth")


@app.get("/dep/raises")
def use_dep_raises(v=Depends(dep_raises)):
    return {"v": v}


def dep_with_request(request: Request):
    return {"method": request.method, "path": request.url.path}


@app.get("/dep/with-request")
def use_dep_req(v=Depends(dep_with_request)):
    return v


def dep_with_query(q: str = "qdefault"):
    return f"dep_q_{q}"


@app.get("/dep/with-query")
def use_dep_query(v=Depends(dep_with_query)):
    return {"v": v}


def dep_with_header(x_custom: str = Header(default="none")):
    return f"dep_h_{x_custom}"


@app.get("/dep/with-header")
def use_dep_header(v=Depends(dep_with_header)):
    return {"v": v}


# ═══════════════════════════════════════════════════════════════════
# Status codes
# ═══════════════════════════════════════════════════════════════════

@app.get("/status/201", status_code=201)
def status_201():
    return {"created": True}


@app.get("/status/204", status_code=204)
def status_204():
    return None


@app.get("/status/418", status_code=418)
def status_418():
    return {"teapot": True}


@app.get("/status/202", status_code=202)
def status_202():
    return {"accepted": True}


@app.delete("/status/del", status_code=204)
def status_del_204():
    return None


# ═══════════════════════════════════════════════════════════════════
# Trailing slashes / redirects
# ═══════════════════════════════════════════════════════════════════

@app.get("/slashtest")
def slashtest():
    return {"path": "no_slash"}


@app.get("/slashtest2/")
def slashtest2():
    return {"path": "with_slash"}


# ═══════════════════════════════════════════════════════════════════
# Large body / large response
# ═══════════════════════════════════════════════════════════════════

@app.post("/body/large")
async def body_large(request: Request):
    body = await request.body()
    return {"len": len(body)}


@app.get("/resp/huge-list")
def resp_huge_list():
    return list(range(5000))


@app.get("/resp/with-ints")
def resp_ints():
    return {"nums": [i * 1000 for i in range(100)]}


# ═══════════════════════════════════════════════════════════════════
# More streaming
# ═══════════════════════════════════════════════════════════════════

@app.get("/stream/incremental")
def stream_incremental():
    def gen():
        for i in range(20):
            yield f"line{i}\n"
    return StreamingResponse(gen(), media_type="text/plain")


@app.get("/stream/json-lines")
def stream_jsonl():
    def gen():
        for i in range(5):
            yield json.dumps({"i": i}) + "\n"
    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/stream/large")
def stream_large():
    def gen():
        for i in range(100):
            yield "x" * 100
    return StreamingResponse(gen(), media_type="text/plain")


# ═══════════════════════════════════════════════════════════════════
# Multi-method endpoints
# ═══════════════════════════════════════════════════════════════════

@app.api_route("/multi-method/echo", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def multi_method_echo(request: Request):
    try:
        body = await request.body()
        return {"method": request.method, "has_body": len(body) > 0}
    except Exception:
        return {"method": request.method, "has_body": False}


# ═══════════════════════════════════════════════════════════════════
# Mount/root path (skipped — mounting sub-app is complex)
# ═══════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════
# Body validation edge cases
# ═══════════════════════════════════════════════════════════════════

class StrictModel(BaseModel):
    name: str
    count: int
    price: float


@app.post("/body/strict")
def body_strict(item: StrictModel):
    return item.model_dump()


@app.post("/body/list-items")
def body_list_items(items: list[Item]):
    return {"count": len(items), "names": [i.name for i in items]}


@app.post("/body/list-ints")
def body_list_ints(nums: list[int] = Body(...)):
    return {"sum": sum(nums)}


@app.post("/body/dict-body")
def body_dict(data: dict = Body(...)):
    return {"keys": sorted(list(data.keys()))}


@app.post("/body/optional")
def body_optional(item: Optional[Item] = None):
    if item is None:
        return {"item": None}
    return {"item": item.model_dump()}


# ═══════════════════════════════════════════════════════════════════
# Form endpoints
# ═══════════════════════════════════════════════════════════════════

@app.post("/form/simple")
def form_simple(a: str = Form(...), b: str = Form(...)):
    return {"a": a, "b": b}


@app.post("/form/with-default")
def form_default(a: str = Form(...), b: str = Form(default="defaultB")):
    return {"a": a, "b": b}


# ═══════════════════════════════════════════════════════════════════
# More cookies
# ═══════════════════════════════════════════════════════════════════

@app.get("/cookie/multi-get")
def cookie_multi_get(
    a: str = Cookie(default="A_D"),
    b: str = Cookie(default="B_D"),
    c: str = Cookie(default="C_D"),
):
    return {"a": a, "b": b, "c": c}


@app.get("/cookie/cookie-and-query")
def cookie_and_query(
    c: str = Cookie(default="cd"),
    q: str = Query(default="qd"),
):
    return {"c": c, "q": q}


# ═══════════════════════════════════════════════════════════════════
# More request introspection
# ═══════════════════════════════════════════════════════════════════

@app.get("/req/url-full")
def req_url_full(request: Request):
    return {
        "str": str(request.url),
        "path": request.url.path,
        "hostname": request.url.hostname,
    }


@app.get("/req/body-none")
async def req_body_none(request: Request):
    body = await request.body()
    return {"len": len(body), "is_empty": len(body) == 0}


@app.get("/req/headers-count")
def req_headers_count(request: Request):
    return {"count": len(list(request.headers.keys()))}


# ═══════════════════════════════════════════════════════════════════
# Multi-cookie set
# ═══════════════════════════════════════════════════════════════════

@app.get("/cookie/set-with-all")
def cookie_all_attrs(response: Response):
    response.set_cookie(
        key="cmplx", value="V",
        max_age=600, path="/x", domain="example.org",
        secure=True, httponly=True, samesite="strict"
    )
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════
# Response models with extra
# ═══════════════════════════════════════════════════════════════════

class RMA(BaseModel):
    a: int
    b: str = "default_b"


@app.get("/rm/exclude-unset", response_model=RMA, response_model_exclude_unset=True)
def rm_exclude_unset():
    return {"a": 1}  # no b → excluded


class RMB(BaseModel):
    a: int
    b: Optional[str] = None


@app.get("/rm/exclude-none", response_model=RMB, response_model_exclude_none=True)
def rm_exclude_none():
    return {"a": 1, "b": None}


@app.get("/rm/by-alias", response_model=RMA, response_model_by_alias=True)
def rm_by_alias():
    return {"a": 1, "b": "hello"}


# ═══════════════════════════════════════════════════════════════════
# OpenAPI route access
# ═══════════════════════════════════════════════════════════════════

# openapi.json endpoint is added by FastAPI automatically — tested in runner.


# ═══════════════════════════════════════════════════════════════════
# Many small GET endpoints for concurrency stress
# ═══════════════════════════════════════════════════════════════════

for _i in range(20):
    _path = f"/stress/ep{_i}"

    def _make_handler(i):
        def _h():
            return {"ep": i}
        return _h

    app.get(_path)(_make_handler(_i))


# ═══════════════════════════════════════════════════════════════════
# Echo endpoints
# ═══════════════════════════════════════════════════════════════════

@app.get("/echo/query")
def echo_query(request: Request):
    return dict(request.query_params)


@app.post("/echo/headers")
async def echo_headers(request: Request):
    return {k: v for k, v in request.headers.items()}


# ═══════════════════════════════════════════════════════════════════
# Middleware-only endpoint test
# ═══════════════════════════════════════════════════════════════════

@app.get("/no-mw-special")
def no_mw_special():
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════
# Fin
# ═══════════════════════════════════════════════════════════════════
