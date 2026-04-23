"""ROUND 2 deep behavior parity app.

Heavily instrumented endpoints. Each request produces an X-Trace response header
containing a JSON array of trace entries. Tests compare the FULL trace between
FastAPI and fastapi-turbo, not just end state.

This app uses ONLY stock FastAPI imports — no fastapi_turbo-specific code. The
compat shim substitutes fastapi-turbo at run time.

The trace records:
  - middleware enter / exit ordering
  - dependency setup / teardown ordering with counters
  - streaming chunk boundaries and timings
  - cookie attribute shape
  - request scope introspection

Instrumentation pattern:
  A thread-local trace list is created per request in the outermost middleware
  and attached to request.state.trace. Handlers/deps append strings. The outer
  middleware serialises it into an X-Trace JSON header on the response.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import time
import threading
from contextlib import asynccontextmanager
from typing import Annotated, Optional, Any

from fastapi import (
    FastAPI, APIRouter, Depends, Query, Path, Header, Cookie, Body, Form,
    File, UploadFile, HTTPException, Request, Response, BackgroundTasks,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    JSONResponse, HTMLResponse, PlainTextResponse, RedirectResponse,
    StreamingResponse, Response as StarletteResponse,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

try:
    from starlette.background import BackgroundTask
except Exception:
    from fastapi.background import BackgroundTasks as BackgroundTask  # fallback

from pydantic import BaseModel, Field


# ════════════════════════════════════════════════════════════════════
# Trace helpers — per-request trace accumulator
# ════════════════════════════════════════════════════════════════════

def _trace_push(request: Request, entry: Any) -> None:
    """Append a trace entry to request.state.trace (init if missing)."""
    if not hasattr(request.state, "trace") or request.state.trace is None:
        request.state.trace = []
    request.state.trace.append(entry)


def _trace_get(request: Request) -> list:
    return getattr(request.state, "trace", None) or []


def _trace_json(request: Request) -> str:
    return json.dumps(_trace_get(request), default=str)


# Lifespan events (module-level)
_lifespan_events: list = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    _lifespan_events.append("startup_begin")
    app.state.db = {"counter": 0}
    app.state.name = "r2_deep_app"
    app.state.started_at = time.time()
    _lifespan_events.append("startup_end")
    yield
    _lifespan_events.append("shutdown_begin")
    _lifespan_events.append("shutdown_end")


app = FastAPI(
    title="Deep Behavior Parity R2",
    version="2.0.0",
    lifespan=lifespan,
)


# ════════════════════════════════════════════════════════════════════
# Global state — all reset via /_reset
# ════════════════════════════════════════════════════════════════════

_call_counts: dict = {}
_yield_events: list = []
_bg_log: list = []
_router_seen: list = []
_state_lock = threading.Lock()


def bump(key: str) -> int:
    with _state_lock:
        _call_counts[key] = _call_counts.get(key, 0) + 1
        return _call_counts[key]


def get_count(key: str) -> int:
    with _state_lock:
        return _call_counts.get(key, 0)


# ════════════════════════════════════════════════════════════════════
# 5 Middlewares — first registered runs INNERMOST
# Order from outermost to innermost (Starlette):
#   MW5  (last registered)    <- outermost
#     MW4
#       MW3
#         MW2
#           MW1               <- innermost
#             handler
# Request path:  MW5_in → MW4_in → MW3_in → MW2_in → MW1_in → handler
# Response path: MW1_out → MW2_out → MW3_out → MW4_out → MW5_out
# ════════════════════════════════════════════════════════════════════

@app.middleware("http")
async def mw1(request: Request, call_next):
    _trace_push(request, "MW1_in")
    resp = await call_next(request)
    _trace_push(request, "MW1_out")
    resp.headers["X-MW1"] = "1"
    return resp


@app.middleware("http")
async def mw2(request: Request, call_next):
    _trace_push(request, "MW2_in")
    resp = await call_next(request)
    _trace_push(request, "MW2_out")
    resp.headers["X-MW2"] = "1"
    return resp


@app.middleware("http")
async def mw3(request: Request, call_next):
    _trace_push(request, "MW3_in")
    if request.headers.get("X-SC-At-3") == "yes":
        _trace_push(request, "MW3_short_circuit")
        # Manually embed trace — further middlewares won't see this response.
        return JSONResponse(
            {"sc_at": 3, "trace": _trace_get(request)},
            status_code=299,
            headers={"X-Trace": _trace_json(request)},
        )
    try:
        resp = await call_next(request)
    except Exception as e:
        _trace_push(request, f"MW3_caught:{type(e).__name__}")
        # rethrow
        raise
    _trace_push(request, "MW3_out")
    resp.headers["X-MW3"] = "1"
    return resp


@app.middleware("http")
async def mw4(request: Request, call_next):
    _trace_push(request, "MW4_in")
    if request.headers.get("X-Raise-At-4") == "yes":
        _trace_push(request, "MW4_will_raise")
        raise RuntimeError("mw4 raised")
    resp = await call_next(request)
    _trace_push(request, "MW4_out")
    resp.headers["X-MW4"] = "1"
    return resp


@app.middleware("http")
async def mw5_outer(request: Request, call_next):
    """Outermost trace — SETS UP the trace list and serialises it out."""
    # initialise trace fresh (guard against middleware that pre-populated)
    request.state.trace = []
    _trace_push(request, "MW5_in")
    if request.headers.get("X-SC-At-5") == "yes":
        _trace_push(request, "MW5_short_circuit")
        return JSONResponse(
            {"sc_at": 5, "trace": _trace_get(request)},
            status_code=299,
            headers={"X-Trace": _trace_json(request)},
        )
    try:
        resp = await call_next(request)
    except Exception as e:
        _trace_push(request, f"MW5_caught:{type(e).__name__}")
        return JSONResponse(
            {"error": str(e), "trace": _trace_get(request)},
            status_code=500,
            headers={"X-Trace": _trace_json(request)},
        )
    _trace_push(request, "MW5_out")
    resp.headers["X-MW5"] = "1"
    # serialise trace into response header
    try:
        resp.headers["X-Trace"] = _trace_json(request)
    except Exception:
        pass
    return resp


# add_middleware layers — sit OUTSIDE the @app.middleware decorators
# when using Starlette's build_middleware_stack. i.e. they're MORE outer.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=1000)


# ════════════════════════════════════════════════════════════════════
# Dependencies with deep instrumentation
# ════════════════════════════════════════════════════════════════════

def dep_a(request: Request):
    n = bump("A")
    _trace_push(request, f"dep_A#{n}")
    return {"dep": "A", "n": n}


def dep_b(request: Request, a=Depends(dep_a)):
    n = bump("B")
    _trace_push(request, f"dep_B#{n}[a={a['n']}]")
    return {"dep": "B", "n": n, "a": a}


def dep_c(request: Request, a=Depends(dep_a)):
    n = bump("C")
    _trace_push(request, f"dep_C#{n}[a={a['n']}]")
    return {"dep": "C", "n": n, "a": a}


def dep_root(request: Request, b=Depends(dep_b), c=Depends(dep_c)):
    n = bump("R")
    _trace_push(request, f"dep_R#{n}")
    return {"root": True, "b": b, "c": c, "n": n}


# Non-cached variants
def dep_nc(request: Request):
    n = bump("NC")
    _trace_push(request, f"dep_NC#{n}")
    return {"n": n}


# Nested yield deps
def dep_ya(request: Request):
    _trace_push(request, "yA_setup")
    bump("ya_setup")
    try:
        yield "YA"
        _trace_push(request, "yA_yielded_ok")
    except Exception as e:
        _trace_push(request, f"yA_exc:{type(e).__name__}")
        raise
    finally:
        _trace_push(request, "yA_teardown")
        bump("ya_teardown")


def dep_yb(request: Request, a=Depends(dep_ya)):
    _trace_push(request, "yB_setup")
    bump("yb_setup")
    try:
        yield f"YB[{a}]"
        _trace_push(request, "yB_yielded_ok")
    except Exception as e:
        _trace_push(request, f"yB_exc:{type(e).__name__}")
        raise
    finally:
        _trace_push(request, "yB_teardown")
        bump("yb_teardown")


def dep_yc(request: Request, b=Depends(dep_yb)):
    _trace_push(request, "yC_setup")
    bump("yc_setup")
    try:
        yield f"YC[{b}]"
        _trace_push(request, "yC_yielded_ok")
    finally:
        _trace_push(request, "yC_teardown")
        bump("yc_teardown")


def dep_yd_that_raises_teardown(request: Request):
    _trace_push(request, "yD_setup")
    try:
        yield "YD"
    finally:
        _trace_push(request, "yD_teardown_will_raise")
        raise RuntimeError("teardown bomb")


# Async yield dep
async def dep_y_async(request: Request):
    _trace_push(request, "y_async_setup")
    await asyncio.sleep(0)
    try:
        yield "AS"
    finally:
        _trace_push(request, "y_async_teardown")


# Dep that uses Request.state set by earlier middleware/dep
def dep_reads_state(request: Request):
    _trace_push(request, f"dep_reads_state:{getattr(request.state, 'marker', 'none')}")
    return getattr(request.state, "marker", None)


def dep_sets_state(request: Request):
    request.state.marker = "set_by_dep"
    _trace_push(request, "dep_sets_state")
    return "set"


def dep_with_query(request: Request, q: str = Query(default="qdef")):
    _trace_push(request, f"dep_with_query:{q}")
    return q


def dep_with_header(request: Request, x_req_id: str = Header(default="none")):
    _trace_push(request, f"dep_with_header:{x_req_id}")
    return x_req_id


# ════════════════════════════════════════════════════════════════════
# Utility reset endpoints
# ════════════════════════════════════════════════════════════════════

@app.get("/_reset")
def reset_all():
    with _state_lock:
        _call_counts.clear()
    _yield_events.clear()
    _bg_log.clear()
    _router_seen.clear()
    return {"reset": True}


@app.get("/_counts")
def get_counts():
    with _state_lock:
        return dict(_call_counts)


@app.get("/_bg_log")
def get_bg_log():
    return {"log": list(_bg_log)}


@app.get("/_yield_events")
def get_yield_events():
    return {"events": list(_yield_events)}


@app.get("/_lifespan")
def get_lifespan():
    return {"events": list(_lifespan_events)}


@app.get("/health")
def health():
    return {"status": "ok"}


# ════════════════════════════════════════════════════════════════════
# Middleware trace endpoints
# ════════════════════════════════════════════════════════════════════

@app.get("/mw/trace")
def mw_trace(request: Request):
    _trace_push(request, "handler")
    return {"got_here": True, "trace_so_far": _trace_get(request)}


@app.get("/mw/raise")
def mw_raise(request: Request):
    _trace_push(request, "handler_will_raise")
    raise RuntimeError("handler boom")


@app.get("/mw/http-exc")
def mw_http_exc(request: Request):
    _trace_push(request, "handler_http_exc")
    raise HTTPException(status_code=418, detail={"code": "TEA"})


# ════════════════════════════════════════════════════════════════════
# Dependency trace endpoints
# ════════════════════════════════════════════════════════════════════

@app.get("/dep/diamond")
def dep_diamond(request: Request, r=Depends(dep_root)):
    """Diamond DAG: R depends on B, C; both depend on A. A must be called once."""
    _trace_push(request, "handler_diamond")
    return {"r": r, "trace": _trace_get(request)}


@app.get("/dep/no-cache-x3")
def dep_no_cache_x3(
    request: Request,
    a=Depends(dep_nc, use_cache=False),
    b=Depends(dep_nc, use_cache=False),
    c=Depends(dep_nc, use_cache=False),
):
    _trace_push(request, "handler_nocache")
    return {"a": a, "b": b, "c": c, "trace": _trace_get(request)}


@app.get("/dep/cache-x3")
def dep_cache_x3(
    request: Request,
    a=Depends(dep_nc),
    b=Depends(dep_nc),
    c=Depends(dep_nc),
):
    _trace_push(request, "handler_cached")
    return {"a": a, "b": b, "c": c, "trace": _trace_get(request)}


@app.get("/dep/state-relay")
def dep_state_relay(
    request: Request,
    _set=Depends(dep_sets_state),
    m=Depends(dep_reads_state),
):
    _trace_push(request, f"handler_state:{m}")
    return {"marker": m, "trace": _trace_get(request)}


# ════════════════════════════════════════════════════════════════════
# Yield dependency trace endpoints
# ════════════════════════════════════════════════════════════════════

@app.get("/yield/nested")
def yield_nested(request: Request, c=Depends(dep_yc)):
    _trace_push(request, f"handler_yield:{c}")
    return {"got": c, "trace": _trace_get(request)}


@app.get("/yield/nested-and-raise")
def yield_nested_and_raise(request: Request, c=Depends(dep_yc)):
    _trace_push(request, "handler_yield_will_raise")
    raise HTTPException(status_code=500, detail="handler yield raise")


@app.get("/yield/teardown-raises")
def yield_teardown_raises(request: Request, d=Depends(dep_yd_that_raises_teardown)):
    _trace_push(request, "handler_teardown_raises")
    return {"ok": True, "trace": _trace_get(request)}


@app.get("/yield/async")
async def yield_async(request: Request, v=Depends(dep_y_async)):
    _trace_push(request, f"handler_yield_async:{v}")
    return {"v": v, "trace": _trace_get(request)}


# ════════════════════════════════════════════════════════════════════
# Streaming with tagged chunks
# ════════════════════════════════════════════════════════════════════

@app.get("/stream/tagged")
def stream_tagged():
    def gen():
        for i in range(10):
            yield json.dumps({"i": i, "tag": f"chunk-{i:02d}"}) + "\n"
    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/stream/tagged-async")
async def stream_tagged_async():
    async def gen():
        for i in range(10):
            await asyncio.sleep(0)
            yield json.dumps({"i": i, "tag": f"async-{i:02d}"}) + "\n"
    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/stream/sse-full")
def stream_sse_full():
    def gen():
        # proper SSE protocol lines
        yield "retry: 3000\n\n"
        for i in range(5):
            yield f"id: {i}\nevent: tick\ndata: payload-{i}\n\n"
        yield "event: done\ndata: bye\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/stream/with-sleep")
async def stream_with_sleep():
    async def gen():
        for i in range(5):
            await asyncio.sleep(0.01)
            yield f"slept-{i}\n"
    return StreamingResponse(gen(), media_type="text/plain")


@app.get("/stream/raises-mid")
def stream_raises_mid():
    def gen():
        yield "before\n"
        yield "still before\n"
        raise RuntimeError("stream boom")
    return StreamingResponse(gen(), media_type="text/plain")


@app.get("/stream/async-raises-mid")
async def stream_async_raises_mid():
    async def gen():
        yield "a\n"
        yield "b\n"
        raise RuntimeError("async stream boom")
    return StreamingResponse(gen(), media_type="text/plain")


@app.get("/stream/bytes-tagged")
def stream_bytes_tagged():
    def gen():
        for i in range(8):
            yield bytes([i]) * 4  # 4 bytes of 0x00, 0x01, …
    return StreamingResponse(gen(), media_type="application/octet-stream")


@app.get("/stream/headers")
def stream_headers():
    def gen():
        yield "x"
    resp = StreamingResponse(gen(), media_type="text/plain")
    resp.headers["X-Stream-Custom"] = "yes"
    return resp


# ════════════════════════════════════════════════════════════════════
# Cookie trace endpoints — every attribute exercised
# ════════════════════════════════════════════════════════════════════

@app.get("/cookie/full-attrs")
def cookie_full(response: Response):
    response.set_cookie(
        key="sess", value="abc123",
        max_age=3600,
        expires=3600,
        path="/api/v1",
        domain="example.com",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return {"ok": True}


@app.get("/cookie/samesite-none")
def cookie_ss_none(response: Response):
    response.set_cookie(key="ss", value="x", samesite="none", secure=True)
    return {"ok": True}


@app.get("/cookie/samesite-strict")
def cookie_ss_strict(response: Response):
    response.set_cookie(key="ss", value="x", samesite="strict")
    return {"ok": True}


@app.get("/cookie/multi-set-cookie")
def cookie_multi(response: Response):
    response.set_cookie(key="a", value="1")
    response.set_cookie(key="b", value="2")
    response.set_cookie(key="c", value="3")
    return {"ok": True}


@app.get("/cookie/delete")
def cookie_delete(response: Response):
    response.delete_cookie(key="stale")
    return {"ok": True}


@app.get("/cookie/delete-with-path")
def cookie_delete_path(response: Response):
    response.delete_cookie(key="s2", path="/foo")
    return {"ok": True}


@app.get("/cookie/quoted-value")
def cookie_quoted(response: Response):
    # with spaces → Starlette quotes them
    response.set_cookie(key="qv", value="has space")
    return {"ok": True}


@app.get("/cookie/get-multi")
def cookie_get_multi(
    a: str = Cookie(default="A_D"),
    b: str = Cookie(default="B_D"),
    c: str = Cookie(default="C_D"),
):
    return {"a": a, "b": b, "c": c}


@app.get("/cookie/urlenc-value")
def cookie_urlenc(foo: str = Cookie(default="none")):
    return {"foo": foo}


# ════════════════════════════════════════════════════════════════════
# Request surface tests
# ════════════════════════════════════════════════════════════════════

@app.get("/req/url-deep")
def req_url_deep(request: Request):
    u = request.url
    return {
        "scheme": u.scheme,
        "hostname": u.hostname,
        "port": u.port,
        "path": u.path,
        "query": u.query,
        "fragment": getattr(u, "fragment", ""),
        "str": str(u),
    }


@app.get("/req/url-mutate")
def req_url_mutate(request: Request):
    u = request.url
    try:
        iq = u.include_query_params(added="yes", x="1")
        iq_s = str(iq)
    except Exception as e:
        iq_s = f"ERR:{type(e).__name__}"
    try:
        rq = u.replace_query_params(replaced="yes")
        rq_s = str(rq)
    except Exception as e:
        rq_s = f"ERR:{type(e).__name__}"
    return {"include": iq_s, "replace": rq_s}


@app.get("/req/headers-getlist")
def req_headers_getlist(request: Request):
    try:
        multi = request.headers.getlist("x-multi")
    except Exception as e:
        multi = [f"ERR:{type(e).__name__}"]
    return {
        "multi": multi,
        "missing_default": request.headers.get("x-absent", "default_val"),
        "lower": request.headers.get("x-custom"),
        "upper": request.headers.get("X-Custom"),
        "mixed": request.headers.get("X-cUsToM"),
    }


@app.get("/req/headers-iter")
def req_headers_iter(request: Request):
    items = []
    for k, v in request.headers.items():
        items.append([k.lower(), v])
    # Keep only ones the test sets
    filt = [it for it in items if it[0].startswith("x-")]
    return {"items": sorted(filt)}


@app.get("/req/query-getlist")
def req_query_getlist(request: Request):
    try:
        ids = request.query_params.getlist("ids")
    except Exception as e:
        ids = [f"ERR:{type(e).__name__}"]
    return {
        "ids": ids,
        "missing": request.query_params.get("missing", "DEF"),
    }


@app.get("/req/query-empty-vs-missing")
def req_query_empty(request: Request):
    return {
        "x_present": "x" in request.query_params,
        "x_value": request.query_params.get("x", "<<MISSING>>"),
    }


@app.get("/req/cookies-dict")
def req_cookies_dict(request: Request):
    return {"cookies": dict(request.cookies)}


@app.get("/req/client")
def req_client(request: Request):
    c = request.client
    return {
        "has_host": bool(c and c.host),
        "has_port": bool(c and c.port),
        "host_is_loopback": bool(c and c.host in ("127.0.0.1", "localhost", "::1")),
    }


@app.get("/req/scope-keys")
def req_scope_keys(request: Request):
    return {
        "type": request.scope.get("type"),
        "method": request.scope.get("method"),
        "path": request.scope.get("path"),
        "http_version": request.scope.get("http_version") or request.scope.get("http-version"),
        "has_headers": "headers" in request.scope,
        "has_query_string": "query_string" in request.scope,
    }


@app.post("/req/body-bytes")
async def req_body_bytes(request: Request):
    b = await request.body()
    return {"len": len(b), "first": b[:16].decode("utf-8", errors="replace")}


@app.post("/req/body-twice")
async def req_body_twice(request: Request):
    b1 = await request.body()
    b2 = await request.body()
    return {"same": b1 == b2, "len1": len(b1), "len2": len(b2)}


@app.post("/req/json-parsed")
async def req_json_parsed(request: Request):
    data = await request.json()
    return {"parsed": data, "type": type(data).__name__}


@app.post("/req/form-multi")
async def req_form_multi(request: Request):
    form = await request.form()
    try:
        tags = form.getlist("tag")
    except Exception as e:
        tags = [f"ERR:{type(e).__name__}"]
    return {"tags": tags, "name": form.get("name", "none")}


@app.post("/req/stream-body")
async def req_stream_body(request: Request):
    chunks = []
    total = 0
    async for chunk in request.stream():
        chunks.append(len(chunk))
        total += len(chunk)
    return {"chunk_count": len(chunks), "total": total}


@app.get("/req/state-set")
def req_state_set(request: Request):
    request.state.mine = "mine_val"
    return {"mine": request.state.mine}


# ════════════════════════════════════════════════════════════════════
# Response surface tests
# ════════════════════════════════════════════════════════════════════

@app.get("/resp/many-headers")
def resp_many_headers():
    r = Response(content=b"x")
    r.headers["X-One"] = "1"
    r.headers["X-Two"] = "2"
    r.headers.append("X-Dup", "a")
    r.headers.append("X-Dup", "b")
    return r


@app.get("/resp/setdefault")
def resp_setdefault():
    r = Response(content=b"x")
    try:
        r.headers.setdefault("X-SetDef", "first")
        r.headers.setdefault("X-SetDef", "second")  # should NOT overwrite
    except Exception as e:
        r.headers["X-SetDef-Error"] = type(e).__name__
    return r


@app.get("/resp/mutablecopy")
def resp_mutablecopy(request: Request):
    r = Response(content=b"x")
    try:
        mc = r.headers.mutablecopy()
        mc["X-Via-Copy"] = "yes"
        ok = "yes"
    except Exception as e:
        ok = f"ERR:{type(e).__name__}"
    r.headers["X-Copy-Status"] = ok
    return r


@app.get("/resp/media-type-override")
def resp_mt_override():
    return Response(content=b"<root/>", media_type="application/xml")


@app.get("/resp/background-task")
def resp_background_single():
    bt = BackgroundTask(_bg_log.append, "single_bg_fired")
    return Response(content=b'{"ok":true}', media_type="application/json", background=bt)


@app.get("/resp/background-tasks-multi")
def resp_background_multi(tasks: BackgroundTasks):
    tasks.add_task(_bg_log.append, "bg_t1")
    tasks.add_task(_bg_log.append, "bg_t2")
    tasks.add_task(_bg_log.append, "bg_t3")
    return {"ok": True}


async def _async_bg(msg: str):
    _bg_log.append(f"async:{msg}")


@app.get("/resp/background-async")
def resp_background_async(tasks: BackgroundTasks):
    tasks.add_task(_async_bg, "hello")
    return {"ok": True}


def _raising_bg():
    _bg_log.append("raising_bg_fired")
    raise RuntimeError("bg boom")


@app.get("/resp/background-raises")
def resp_background_raises(tasks: BackgroundTasks):
    tasks.add_task(_raising_bg)
    tasks.add_task(_bg_log.append, "after_raise")
    return {"ok": True}


# ════════════════════════════════════════════════════════════════════
# UploadFile
# ════════════════════════════════════════════════════════════════════

@app.post("/upload/one")
async def upload_one(file: UploadFile = File(...)):
    data = await file.read()
    return {
        "filename": file.filename,
        "content_type": file.content_type,
        "size": len(data),
        "first8": data[:8].decode("utf-8", errors="replace"),
    }


@app.post("/upload/seek")
async def upload_seek(file: UploadFile = File(...)):
    data1 = await file.read()
    try:
        await file.seek(0)
        data2 = await file.read()
        seek_ok = True
    except Exception as e:
        data2 = b""
        seek_ok = f"ERR:{type(e).__name__}"
    await file.close()
    return {"same": data1 == data2, "seek_ok": seek_ok, "size": len(data1)}


@app.post("/upload/multi")
async def upload_multi(files: list[UploadFile] = File(...)):
    out = []
    for f in files:
        d = await f.read()
        out.append({"name": f.filename, "size": len(d)})
    return {"files": out}


@app.post("/upload/unicode-name")
async def upload_unicode(file: UploadFile = File(...)):
    return {"filename": file.filename}


# ════════════════════════════════════════════════════════════════════
# Concurrency endpoints
# ════════════════════════════════════════════════════════════════════

_concurrency_counter = {"n": 0}
_concurrency_lock = threading.Lock()


@app.get("/concurrency/slow")
async def concurrency_slow(request: Request):
    await asyncio.sleep(0.01)
    with _concurrency_lock:
        _concurrency_counter["n"] += 1
        n = _concurrency_counter["n"]
    return {"n": n, "req_id": request.headers.get("X-Req-Id", "")}


@app.get("/concurrency/counter")
def concurrency_get_counter():
    return {"n": _concurrency_counter["n"]}


@app.get("/concurrency/reset")
def concurrency_reset():
    with _concurrency_lock:
        _concurrency_counter["n"] = 0
    return {"reset": True}


@app.get("/concurrency/scoped-dep")
def concurrency_scoped_dep(request: Request, a=Depends(dep_a)):
    """Dep runs once per request; returns its own counter value."""
    return {"a_call": a["n"], "req_id": request.headers.get("X-Req-Id", "")}


@app.post("/concurrency/req-state")
async def concurrency_req_state(request: Request):
    """Handler sets request.state then reads it back — asserts no cross-request bleed."""
    req_id = request.headers.get("X-Req-Id", "NONE")
    request.state.private = req_id
    await asyncio.sleep(0.005)
    return {"in": req_id, "out": request.state.private, "match": req_id == request.state.private}


# ════════════════════════════════════════════════════════════════════
# Exception handler wiring
# ════════════════════════════════════════════════════════════════════

class MyCustomError(Exception):
    def __init__(self, code: str, msg: str):
        self.code = code
        self.msg = msg


@app.exception_handler(MyCustomError)
async def my_custom_handler(request: Request, exc: MyCustomError):
    return JSONResponse(
        {"custom_error": exc.code, "msg": exc.msg},
        status_code=418,
    )


@app.exception_handler(RequestValidationError)
async def custom_rv_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        {"custom_rv": True, "errors": [str(e.get("type", "")) for e in exc.errors()]},
        status_code=422,
    )


@app.get("/exc/custom")
def raise_custom():
    raise MyCustomError("E_X", "something")


@app.get("/exc/http-with-dict")
def raise_http_dict():
    raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "where": "db"})


@app.get("/exc/http-with-list")
def raise_http_list():
    raise HTTPException(status_code=400, detail=[{"a": 1}, {"b": 2}])


@app.get("/exc/http-with-headers")
def raise_http_hdr():
    raise HTTPException(status_code=401, detail="unauth",
                        headers={"WWW-Authenticate": "Bearer realm=x"})


@app.get("/exc/value-error")
def raise_value():
    raise ValueError("generic")


class StrictBody(BaseModel):
    name: str
    age: int


@app.post("/exc/strict-body")
def exc_strict(body: StrictBody):
    return body.model_dump()


# ════════════════════════════════════════════════════════════════════
# Starlette surface (MutableHeaders, URL, QueryParams)
# ════════════════════════════════════════════════════════════════════

@app.get("/starlette/url-dict")
def starlette_url_dict(request: Request):
    u = request.url
    try:
        return {
            "path": u.path,
            "host": u.hostname,
            "scheme": u.scheme,
            "userinfo": getattr(u, "userinfo", ""),
        }
    except Exception as e:
        return {"err": str(e)}


@app.get("/starlette/headers-dict")
def starlette_headers_dict(request: Request):
    d = {}
    for k, v in request.headers.items():
        d[k] = v
    return {"has_host": "host" in d, "count_ge_1": len(d) >= 1}


@app.get("/starlette/qp-dict")
def starlette_qp_dict(request: Request):
    return {"as_dict": dict(request.query_params)}


@app.get("/starlette/qp-multi")
def starlette_qp_multi(request: Request):
    # multi_items is Starlette-specific
    try:
        return {"multi": list(request.query_params.multi_items())}
    except Exception as e:
        return {"err": str(e)}


# ════════════════════════════════════════════════════════════════════
# State/lifespan
# ════════════════════════════════════════════════════════════════════

@app.get("/state/db")
def state_db(request: Request):
    return {"name": request.app.state.name,
            "db": dict(request.app.state.db)}


@app.get("/state/incr")
def state_incr(request: Request):
    request.app.state.db["counter"] += 1
    return {"counter": request.app.state.db["counter"]}


# ════════════════════════════════════════════════════════════════════
# BackgroundTasks ordering
# ════════════════════════════════════════════════════════════════════

@app.get("/bg/ordered-5")
def bg_ordered_5(tasks: BackgroundTasks):
    for i in range(5):
        tasks.add_task(_bg_log.append, f"ord-{i}")
    return {"ok": True}


@app.get("/bg/clear")
def bg_clear():
    _bg_log.clear()
    return {"cleared": True}


# ════════════════════════════════════════════════════════════════════
# Per-request trace sanity — echo full trace
# ════════════════════════════════════════════════════════════════════

@app.get("/trace/five-deps")
def trace_five_deps(
    request: Request,
    a=Depends(dep_a),
    b=Depends(dep_b),
    c=Depends(dep_c),
    r=Depends(dep_root),
    v=Depends(dep_with_query),
):
    _trace_push(request, "handler_five")
    return {"trace": _trace_get(request)}


# ════════════════════════════════════════════════════════════════════
# Sub-app / router-with-deps
# ════════════════════════════════════════════════════════════════════

def router_dep_fn(request: Request):
    _router_seen.append("router_dep")
    _trace_push(request, "router_dep")
    return "router_dep"


sub_router = APIRouter(prefix="/sub", dependencies=[Depends(router_dep_fn)])


@sub_router.get("/a")
def sub_a(request: Request):
    _trace_push(request, "sub_a_handler")
    return {"r": "a", "trace": _trace_get(request)}


@sub_router.get("/b")
def sub_b(request: Request):
    _trace_push(request, "sub_b_handler")
    return {"r": "b", "trace": _trace_get(request)}


app.include_router(sub_router)


# ════════════════════════════════════════════════════════════════════
# Misc edge cases — MANY small endpoints mirroring R1 but each
# returns more information
# ════════════════════════════════════════════════════════════════════

@app.get("/misc/header-dupe")
def misc_header_dupe():
    r = JSONResponse({"ok": True})
    r.headers.append("X-Dupe", "one")
    r.headers.append("X-Dupe", "two")
    r.headers.append("X-Dupe", "three")
    return r


@app.get("/misc/unicode-json")
def misc_unicode_json():
    return {
        "emoji": "hi there",
        "chinese": "你好",
        "japanese": "こんにちは",
        "rtl": "שלום",
        "accented": "café",
        "mixed": "A你B好C",
    }


@app.get("/misc/large-list")
def misc_large_list():
    return {"n": list(range(500))}


@app.get("/misc/deep-json")
def misc_deep_json():
    d = {"v": 0}
    for _ in range(30):
        d = {"n": d}
    return d


@app.get("/misc/special-chars")
def misc_special_chars():
    return {"s": 'he said "hi" then \\ then \n and \t'}


@app.get("/misc/numeric-edges")
def misc_numeric():
    return {
        "zero": 0,
        "neg": -1,
        "max_i32": 2**31 - 1,
        "max_i53": 2**53 - 1,
        "pi": 3.141592653589793,
        "neg_pi": -3.141592653589793,
    }


# ════════════════════════════════════════════════════════════════════
# Form + File together
# ════════════════════════════════════════════════════════════════════

@app.post("/form+file")
async def form_and_file(
    name: str = Form(...),
    file: UploadFile = File(...),
):
    data = await file.read()
    return {"name": name, "filename": file.filename, "size": len(data)}


# ════════════════════════════════════════════════════════════════════
# HTTP verbs — trace_marker included
# ════════════════════════════════════════════════════════════════════

@app.api_route("/verbs/any", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def verbs_any(request: Request):
    try:
        body = await request.body()
    except Exception:
        body = b""
    return {"method": request.method, "len": len(body)}


# ════════════════════════════════════════════════════════════════════
# Response model edges
# ════════════════════════════════════════════════════════════════════

class RM(BaseModel):
    a: int
    b: Optional[str] = None


@app.get("/rm/exclude-none", response_model=RM, response_model_exclude_none=True)
def rm_exclude_none():
    return {"a": 1, "b": None}


@app.get("/rm/exclude-unset", response_model=RM, response_model_exclude_unset=True)
def rm_exclude_unset():
    return {"a": 1}


class Item(BaseModel):
    name: str
    price: float


@app.post("/rm/echo", response_model=Item)
def rm_echo(item: Item):
    return item


# ════════════════════════════════════════════════════════════════════
# Lots of small concurrency endpoints
# ════════════════════════════════════════════════════════════════════

@app.get("/ep/echo/{n}")
def ep_echo(n: int):
    return {"n": n, "doubled": n * 2}


@app.get("/ep/fast")
def ep_fast():
    return {"fast": True}


@app.get("/ep/async-fast")
async def ep_async_fast():
    return {"async_fast": True}


# ════════════════════════════════════════════════════════════════════
# Path / Query / Header extraction with extra depth
# ════════════════════════════════════════════════════════════════════

@app.get("/pp/int/{x}")
def pp_int(x: int):
    return {"x": x, "t": "int"}


@app.get("/pp/str/{x}")
def pp_str(x: str):
    return {"x": x, "t": "str"}


@app.get("/pp/path/{p:path}")
def pp_path(p: str):
    return {"p": p}


@app.get("/pp/list-query")
def pp_list_query(tag: list[str] = Query(default=[])):
    return {"tags": tag, "count": len(tag)}


@app.get("/pp/alias-query")
def pp_alias_query(my_val: str = Query(default="d", alias="myVal")):
    return {"v": my_val}


@app.get("/pp/header-alias")
def pp_header_alias(custom: str = Header(default="d", alias="X-Custom-Alias")):
    return {"h": custom}


@app.get("/pp/header-underscore")
def pp_header_underscore(x_custom: str = Header(default="d")):
    return {"h": x_custom}


@app.get("/pp/bool-query")
def pp_bool_query(flag: bool = Query(default=False)):
    return {"flag": flag}


@app.get("/pp/int-query")
def pp_int_query(n: int = Query(default=0)):
    return {"n": n}


@app.get("/pp/float-query")
def pp_float_query(p: float = Query(default=0.0)):
    return {"p": p}


@app.get("/pp/numeric-constraints")
def pp_numeric(age: int = Query(ge=0, le=150)):
    return {"age": age}


# ════════════════════════════════════════════════════════════════════
# Status codes
# ════════════════════════════════════════════════════════════════════

@app.get("/status/201", status_code=201)
def s201():
    return {"created": True}


@app.get("/status/204", status_code=204)
def s204():
    return None


@app.get("/status/418", status_code=418)
def s418():
    return {"teapot": True}


# ════════════════════════════════════════════════════════════════════
# 30 small endpoints for load
# ════════════════════════════════════════════════════════════════════

for _i in range(30):
    def _mkh(i):
        def _h():
            return {"ep": i, "ok": True}
        return _h
    app.get(f"/load/ep{_i}")(_mkh(_i))
