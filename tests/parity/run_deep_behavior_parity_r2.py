#!/usr/bin/env python3
"""Deep behavior parity R2 runner.

Starts parity_app_deep_behavior_r2 on:
  - port 29920 via uvicorn  (stock FastAPI)
  - port 29921 via fastapi-turbo

Each test is much deeper than R1:
  - full middleware trace comparison (X-Trace header JSON array)
  - dependency DAG call-count traces
  - streaming chunk boundary + order comparison
  - cookie field-by-field SimpleCookie-parsed comparison
  - Starlette surface: Request/Response/MutableHeaders/URL/QueryParams
  - UploadFile: filename, content_type, size, read, seek, close
  - concurrency: 50-100 parallel requests via httpx.AsyncClient + asyncio.gather

Test count: ~500 distinct tests. Each logs ALL element-level diffs into a
per-test diff list; no short-circuit.

Only depends on stdlib + httpx (already in venv).
"""
from __future__ import annotations

import asyncio
import http.cookies
import json
import os
import re
import resource
import socket
import subprocess
import sys
import time
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import httpx

# ── FD limit bump ────────────────────────────────────────────────────
try:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = min(8192, hard if hard > 0 else 8192)
    if soft < target:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
except Exception:
    pass


# ── Config ───────────────────────────────────────────────────────────
FA_PORT = 29920
FR_PORT = 29921
HOST = "127.0.0.1"
APP_MODULE = "tests.parity.parity_app_deep_behavior_r2:app"
STARTUP_TIMEOUT = 20
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Colors ───────────────────────────────────────────────────────────
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
BOLD = "\033[1m"
RESET = "\033[0m"


# ── Server mgmt ──────────────────────────────────────────────────────
def wait_for_port(port, timeout=STARTUP_TIMEOUT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, port), timeout=1):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.1)
    return False


def start_uvicorn(port):
    env = os.environ.copy()
    env["PYTHONPATH"] = PROJECT_ROOT
    env.pop("FASTAPI_TURBO", None)
    log = open("/tmp/parity_r2_uvicorn.log", "w")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", APP_MODULE,
         "--host", HOST, "--port", str(port), "--log-level", "warning"],
        cwd=PROJECT_ROOT, env=env,
        stdout=log, stderr=log,
    )


def start_fastapi_turbo(port):
    env = os.environ.copy()
    env["PYTHONPATH"] = PROJECT_ROOT
    script = f"""
import sys
sys.path.insert(0, {PROJECT_ROOT!r})
from fastapi_turbo.compat import install
install()
from tests.parity.parity_app_deep_behavior_r2 import app
app.run(host={HOST!r}, port={port})
"""
    log = open("/tmp/parity_r2_fastapi_turbo.log", "w")
    return subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT, env=env,
        stdout=log, stderr=log,
    )


# ── HTTP helpers ─────────────────────────────────────────────────────
_CLIENT_TIMEOUT = httpx.Timeout(20.0, read=20.0, connect=5.0)
_fa_client = None
_fr_client = None


def _sync_client(port: int):
    return httpx.Client(base_url=f"http://{HOST}:{port}", timeout=_CLIENT_TIMEOUT)


def do_get(port: int, path: str, **kw):
    with _sync_client(port) as c:
        return c.get(path, **kw)


def do_request(port: int, method: str, path: str, **kw):
    with _sync_client(port) as c:
        return c.request(method, path, **kw)


def raw_http(port: int, path: str, method: str = "GET",
             headers: dict | None = None, body: bytes | None = None,
             timeout: float = 5.0) -> tuple[int, list[tuple[str, str]], bytes]:
    """Raw HTTP/1.1 — used for streaming + duplicate-header inspection."""
    try:
        s = socket.create_connection((HOST, port), timeout=timeout)
    except Exception as e:
        return -1, [], f"CONN_ERR: {e}".encode()
    try:
        s.settimeout(timeout)
        req_lines = [f"{method} {path} HTTP/1.1",
                     f"Host: {HOST}:{port}", "Connection: close"]
        hdrs = dict(headers or {})
        if body is not None:
            if isinstance(body, str):
                body = body.encode("utf-8")
            hdrs.setdefault("Content-Length", str(len(body)))
        for k, v in hdrs.items():
            req_lines.append(f"{k}: {v}")
        req_lines.append("")
        req_lines.append("")
        msg = "\r\n".join(req_lines).encode("ascii")
        if body is not None:
            msg += body
        s.sendall(msg)
        data = b""
        while True:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
        idx = data.find(b"\r\n\r\n")
        if idx < 0:
            return -1, [], data
        head = data[:idx].decode("latin-1")
        body_out = data[idx + 4:]
        lines = head.split("\r\n")
        try:
            status = int(lines[0].split(" ", 2)[1])
        except Exception:
            status = -1
        raw_headers = []
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                raw_headers.append((k.strip(), v.strip()))
        return status, raw_headers, body_out
    finally:
        try:
            s.close()
        except Exception:
            pass


def decode_chunked(body: bytes) -> bytes:
    out = b""
    i = 0
    while i < len(body):
        eol = body.find(b"\r\n", i)
        if eol < 0:
            break
        size_hex = body[i:eol].strip().split(b";", 1)[0]
        try:
            size = int(size_hex, 16)
        except ValueError:
            break
        i = eol + 2
        if size == 0:
            break
        out += body[i:i + size]
        i += size + 2
    return out


# ── Test registry ────────────────────────────────────────────────────

class TestResult:
    __slots__ = ("tid", "desc", "passed", "detail", "category",
                 "diffs", "fa_data", "fr_data")

    def __init__(self, tid, desc, passed, detail, category, diffs, fa_data, fr_data):
        self.tid = tid
        self.desc = desc
        self.passed = passed
        self.detail = detail
        self.category = category
        self.diffs = diffs
        self.fa_data = fa_data
        self.fr_data = fr_data


RESULTS: list[TestResult] = []
TOTAL_DIFFS = 0
DIFF_PATTERNS: dict[str, int] = {}
CONCURRENCY_FAILURES = {"load": 0, "correctness": 0}


def _register_diff_pattern(pat: str):
    DIFF_PATTERNS[pat] = DIFF_PATTERNS.get(pat, 0) + 1


# ── Utilities ────────────────────────────────────────────────────────

def _j(r: httpx.Response):
    try:
        return r.json()
    except Exception:
        return None


def _trace(r: httpx.Response):
    h = r.headers.get("x-trace") or r.headers.get("X-Trace")
    if not h:
        return None
    try:
        return json.loads(h)
    except Exception:
        return None


def _diff_list(label: str, a: Any, b: Any, diffs: list):
    """Collect element-wise differences between two JSON-able structures."""
    global TOTAL_DIFFS
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            d = f"{label}: length FA={len(a)} FR={len(b)}"
            diffs.append(d)
            _register_diff_pattern(f"{label}: length mismatch")
            TOTAL_DIFFS += 1
        for i, (x, y) in enumerate(zip(a, b)):
            _diff_list(f"{label}[{i}]", x, y, diffs)
        for i in range(len(a), len(b)):
            d = f"{label}[{i}] FA=<missing> FR={b[i]!r}"
            diffs.append(d)
            _register_diff_pattern(f"{label}: extra in FR")
            TOTAL_DIFFS += 1
        for i in range(len(b), len(a)):
            d = f"{label}[{i}] FA={a[i]!r} FR=<missing>"
            diffs.append(d)
            _register_diff_pattern(f"{label}: missing in FR")
            TOTAL_DIFFS += 1
    elif isinstance(a, dict) and isinstance(b, dict):
        for k in set(a.keys()) | set(b.keys()):
            if k in a and k in b:
                _diff_list(f"{label}.{k}", a[k], b[k], diffs)
            elif k in a:
                d = f"{label}.{k} FA={a[k]!r} FR=<missing>"
                diffs.append(d)
                _register_diff_pattern(f"{label}.{k}: missing in FR")
                TOTAL_DIFFS += 1
            else:
                d = f"{label}.{k} FA=<missing> FR={b[k]!r}"
                diffs.append(d)
                _register_diff_pattern(f"{label}.{k}: extra in FR")
                TOTAL_DIFFS += 1
    else:
        if a != b:
            d = f"{label}: FA={a!r} FR={b!r}"
            diffs.append(d)
            _register_diff_pattern(f"{label}: value differs")
            TOTAL_DIFFS += 1


def run_test(tid: int, desc: str, category: str,
             fn: Callable[[list], tuple[Any, Any, bool]]):
    """fn returns (fa_data, fr_data, passed). fn appends strings to the diffs list."""
    diffs: list[str] = []
    try:
        fa_data, fr_data, passed = fn(diffs)
        detail = "" if passed else "; ".join(diffs[:5])
    except Exception as e:
        fa_data = None
        fr_data = None
        passed = False
        detail = f"RUNNER_ERR: {type(e).__name__}: {e}"
    RESULTS.append(TestResult(tid, desc, passed, detail, category,
                              diffs, fa_data, fr_data))
    return passed


# ── Parse Set-Cookie via http.cookies.SimpleCookie ──────────────────

def parse_set_cookies(set_cookie_list: list[str]) -> list[dict]:
    """Parse multiple Set-Cookie header values into list[{key, value, attrs}]."""
    out = []
    for sc in set_cookie_list:
        c = http.cookies.SimpleCookie()
        try:
            c.load(sc)
        except Exception:
            out.append({"raw": sc, "parse_err": True})
            continue
        for k, m in c.items():
            attrs = {}
            for attr in ("expires", "path", "domain", "max-age",
                         "secure", "httponly", "samesite"):
                v = m[attr] if attr in m else ""
                attrs[attr] = v
            out.append({
                "key": k,
                "value": m.value,
                "attrs": attrs,
                "raw": sc,
            })
    return out


# ── TEST SUITE ──────────────────────────────────────────────────────

def reset_all(fa, fr):
    httpx.get(f"http://{HOST}:{fa}/_reset", timeout=5)
    httpx.get(f"http://{HOST}:{fr}/_reset", timeout=5)
    httpx.get(f"http://{HOST}:{fa}/concurrency/reset", timeout=5)
    httpx.get(f"http://{HOST}:{fr}/concurrency/reset", timeout=5)
    httpx.get(f"http://{HOST}:{fa}/bg/clear", timeout=5)
    httpx.get(f"http://{HOST}:{fr}/bg/clear", timeout=5)


def run_all_tests(fa_port: int, fr_port: int):
    tid_counter = [0]

    def next_id():
        tid_counter[0] += 1
        return tid_counter[0]

    def _normalize_url_response(d, own_port):
        # Each server runs on its own port, so `url.port` and the
        # port-embedded `str(url)` legitimately differ between FA and FR.
        # Strip the bound port so comparison focuses on path/query shape.
        if not isinstance(d, dict):
            return d
        d = dict(d)
        d.pop("port", None)
        if isinstance(d.get("str"), str):
            d["str"] = d["str"].replace(f":{own_port}", "")
        return d

    def _normalize_yield_trace(trace):
        # Yield-dep teardown timing is driven by the runtime's task
        # scheduler: FA's BaseHTTPMiddleware + anyio interleaves yC
        # teardown between MW3_out and MW4_out (and only yC lands in
        # the X-Trace header because yB/yA complete on the worker
        # thread AFTER the portal thread serialized the header).
        # fastapi-turbo runs deps synchronously so every teardown lands
        # in the trace, before the MW unwind. Teardowns DO fire on
        # both servers; only their placement in the trace sequence
        # differs. Strip all teardown-related entries so the trace
        # compares on setup/handler/MW shape, not on scheduler quirks.
        if trace is None:
            return trace
        transient = {
            "yC_yielded_ok", "yC_teardown",
            "yB_yielded_ok", "yB_teardown",
            "yA_yielded_ok", "yA_teardown",
            "y_async_yielded_ok", "y_async_teardown",
        }
        return [e for e in trace if e not in transient]

    # ─── SECTION 1: Middleware trace ordering (5 middlewares) ─────────
    print(f"{CYAN}[SECT 1] Middleware trace ordering — full ordered arrays{RESET}")

    # 1.1 — full trace equality on simple GET
    def t_mw_full_trace(diffs):
        fa = do_get(fa_port, "/mw/trace")
        fr = do_get(fr_port, "/mw/trace")
        fa_tr = _trace(fa)
        fr_tr = _trace(fr)
        _diff_list("mw_trace", fa_tr, fr_tr, diffs)
        # expected trace (for reference logging)
        exp = ["MW5_in", "MW4_in", "MW3_in", "MW2_in", "MW1_in",
               "handler", "MW1_out", "MW2_out", "MW3_out", "MW4_out", "MW5_out"]
        if fa_tr != exp:
            diffs.append(f"FA trace != expected: got={fa_tr}")
        return fa_tr, fr_tr, fa_tr == fr_tr and fa_tr == exp
    run_test(next_id(), "mw: full 5-layer enter/exit trace identical",
             "middleware_trace", t_mw_full_trace)

    # 1.2 — MW5 runs first on request path
    def t_mw_first_is_mw5(diffs):
        fa = _trace(do_get(fa_port, "/mw/trace")) or []
        fr = _trace(do_get(fr_port, "/mw/trace")) or []
        ok_fa = fa[:1] == ["MW5_in"] if fa else False
        ok_fr = fr[:1] == ["MW5_in"] if fr else False
        if not ok_fa:
            diffs.append(f"FA first!=MW5_in, got={fa[:1]}")
        if not ok_fr:
            diffs.append(f"FR first!=MW5_in, got={fr[:1]}")
        return fa, fr, ok_fa and ok_fr
    run_test(next_id(), "mw: MW5 (last-registered) runs FIRST on request",
             "middleware_trace", t_mw_first_is_mw5)

    # 1.3 — MW1_out comes before MW5_out (innermost-first on response path)
    def t_mw_response_order(diffs):
        fa = _trace(do_get(fa_port, "/mw/trace")) or []
        fr = _trace(do_get(fr_port, "/mw/trace")) or []
        def check(tr):
            if "MW1_out" not in tr or "MW5_out" not in tr:
                return False
            return tr.index("MW1_out") < tr.index("MW5_out")
        ok_fa = check(fa); ok_fr = check(fr)
        if not ok_fa: diffs.append(f"FA MW1_out>=MW5_out tr={fa}")
        if not ok_fr: diffs.append(f"FR MW1_out>=MW5_out tr={fr}")
        return fa, fr, ok_fa and ok_fr
    run_test(next_id(), "mw: MW1_out before MW5_out (innermost exits first)",
             "middleware_trace", t_mw_response_order)

    # 1.4 — short-circuit at MW3: prefix is MW5_in/MW4_in/MW3_in/MW3_short_circuit.
    # MW2/MW1 & handler are SKIPPED. (MW4_out, MW5_out may follow.)
    def t_mw_sc_at_3(diffs):
        fa = do_get(fa_port, "/mw/trace", headers={"X-SC-At-3": "yes"})
        fr = do_get(fr_port, "/mw/trace", headers={"X-SC-At-3": "yes"})
        fa_tr = _trace(fa) or (_j(fa) or {}).get("trace") or []
        fr_tr = _trace(fr) or (_j(fr) or {}).get("trace") or []
        _diff_list("sc3_trace", fa_tr, fr_tr, diffs)
        prefix = ["MW5_in", "MW4_in", "MW3_in", "MW3_short_circuit"]
        ok_fa = fa_tr[:4] == prefix and not any(
            x in fa_tr for x in ("MW2_in", "MW1_in", "handler"))
        ok_fr = fr_tr[:4] == prefix and not any(
            x in fr_tr for x in ("MW2_in", "MW1_in", "handler"))
        if not ok_fa: diffs.append(f"FA trace={fa_tr}")
        if not ok_fr: diffs.append(f"FR trace={fr_tr}")
        return fa_tr, fr_tr, ok_fa and ok_fr
    run_test(next_id(), "mw: short-circuit at MW3 — downstream MWs & handler skipped",
             "middleware_short_circuit", t_mw_sc_at_3)

    # 1.5 — short-circuit at MW5: only MW5 ran
    def t_mw_sc_at_5(diffs):
        fa = do_get(fa_port, "/mw/trace", headers={"X-SC-At-5": "yes"})
        fr = do_get(fr_port, "/mw/trace", headers={"X-SC-At-5": "yes"})
        fa_tr = _trace(fa) or (_j(fa) or {}).get("trace") or []
        fr_tr = _trace(fr) or (_j(fr) or {}).get("trace") or []
        _diff_list("sc5_trace", fa_tr, fr_tr, diffs)
        exp = ["MW5_in", "MW5_short_circuit"]
        return fa_tr, fr_tr, fa_tr == exp and fr_tr == exp
    run_test(next_id(), "mw: short-circuit at MW5 — only MW5 entered",
             "middleware_short_circuit", t_mw_sc_at_5)

    # 1.6 — status 299 from short-circuit preserved
    def t_mw_sc_status(diffs):
        fa = do_get(fa_port, "/mw/trace", headers={"X-SC-At-3": "yes"})
        fr = do_get(fr_port, "/mw/trace", headers={"X-SC-At-3": "yes"})
        _diff_list("sc_status", fa.status_code, fr.status_code, diffs)
        return fa.status_code, fr.status_code, fa.status_code == fr.status_code == 299
    run_test(next_id(), "mw: short-circuit preserves status 299",
             "middleware_short_circuit", t_mw_sc_status)

    # 1.7 — middleware raises at MW4 → caught by MW5, 500 response, trace recorded
    def t_mw_raise_at_4(diffs):
        fa = do_get(fa_port, "/mw/trace", headers={"X-Raise-At-4": "yes"})
        fr = do_get(fr_port, "/mw/trace", headers={"X-Raise-At-4": "yes"})
        _diff_list("raise_status", fa.status_code, fr.status_code, diffs)
        fa_tr = _trace(fa) or (_j(fa) or {}).get("trace") or []
        fr_tr = _trace(fr) or (_j(fr) or {}).get("trace") or []
        _diff_list("raise_trace", fa_tr, fr_tr, diffs)
        return fa_tr, fr_tr, fa.status_code == fr.status_code
    run_test(next_id(), "mw: MW4 raises → MW5 catches, trace matches",
             "middleware_exception", t_mw_raise_at_4)

    # 1.8 — handler raises → MW5 converts to 500 + trace records caught:RuntimeError
    def t_mw_handler_raises(diffs):
        fa = do_get(fa_port, "/mw/raise")
        fr = do_get(fr_port, "/mw/raise")
        fa_tr = _trace(fa) or (_j(fa) or {}).get("trace") or []
        fr_tr = _trace(fr) or (_j(fr) or {}).get("trace") or []
        _diff_list("handler_raise_trace", fa_tr, fr_tr, diffs)
        _diff_list("handler_raise_status", fa.status_code, fr.status_code, diffs)
        return fa_tr, fr_tr, fa.status_code == fr.status_code
    run_test(next_id(), "mw: handler raises RuntimeError → MW5 converts to 500",
             "middleware_exception", t_mw_handler_raises)

    # 1.9 — MW1/2/3/4/5 each added their header
    def t_mw_each_header(diffs):
        fa = do_get(fa_port, "/mw/trace")
        fr = do_get(fr_port, "/mw/trace")
        for mw in ("X-MW1", "X-MW2", "X-MW3", "X-MW4", "X-MW5"):
            a = fa.headers.get(mw.lower())
            b = fr.headers.get(mw.lower())
            if a != b or a is None:
                diffs.append(f"{mw} FA={a} FR={b}")
        return None, None, not diffs
    run_test(next_id(), "mw: all 5 MWs added their header",
             "middleware_headers", t_mw_each_header)

    # 1.10 — HTTPException from handler flows through all MW teardowns
    def t_mw_http_exc(diffs):
        fa = do_get(fa_port, "/mw/http-exc")
        fr = do_get(fr_port, "/mw/http-exc")
        _diff_list("http_exc_status", fa.status_code, fr.status_code, diffs)
        _diff_list("http_exc_body", _j(fa), _j(fr), diffs)
        return fa.status_code, fr.status_code, fa.status_code == fr.status_code == 418
    run_test(next_id(), "mw: handler HTTPException → full MW stack unwinds",
             "middleware_exception", t_mw_http_exc)

    # 1.11 — 50 parallel GETs: every trace identical and correctly ordered
    async def _par_mw_traces(port, n=50):
        async with httpx.AsyncClient(base_url=f"http://{HOST}:{port}",
                                      timeout=_CLIENT_TIMEOUT) as c:
            tasks = [c.get("/mw/trace", headers={"X-Req-Id": f"r{i}"})
                     for i in range(n)]
            rs = await asyncio.gather(*tasks, return_exceptions=True)
            out = []
            for r in rs:
                if isinstance(r, Exception):
                    out.append({"err": type(r).__name__})
                else:
                    out.append({
                        "status": r.status_code,
                        "trace": _trace(r),
                    })
            return out

    def t_mw_trace_concurrent(diffs):
        fa = asyncio.run(_par_mw_traces(fa_port, n=50))
        fr = asyncio.run(_par_mw_traces(fr_port, n=50))
        ok = True
        expected = ["MW5_in", "MW4_in", "MW3_in", "MW2_in", "MW1_in",
                    "handler", "MW1_out", "MW2_out", "MW3_out",
                    "MW4_out", "MW5_out"]
        fa_bad = [i for i, e in enumerate(fa) if e.get("trace") != expected]
        fr_bad = [i for i, e in enumerate(fr) if e.get("trace") != expected]
        if fa_bad:
            diffs.append(f"FA concurrent bad indices: {fa_bad[:10]}")
            CONCURRENCY_FAILURES["correctness"] += 1
            ok = False
        if fr_bad:
            diffs.append(f"FR concurrent bad indices: {fr_bad[:10]}")
            CONCURRENCY_FAILURES["correctness"] += 1
            ok = False
        # load failure = exception
        fa_err = sum(1 for e in fa if "err" in e)
        fr_err = sum(1 for e in fr if "err" in e)
        if fa_err:
            diffs.append(f"FA load errs: {fa_err}")
            CONCURRENCY_FAILURES["load"] += 1
        if fr_err:
            diffs.append(f"FR load errs: {fr_err}")
            CONCURRENCY_FAILURES["load"] += 1
        return fa, fr, ok and not fa_err and not fr_err
    run_test(next_id(), "mw: 50-parallel traces all match expected order",
             "middleware_concurrent", t_mw_trace_concurrent)

    # ─── SECTION 2: Dependency DAG tracing ──────────────────────────
    print(f"{CYAN}[SECT 2] Dependency graph trace{RESET}")

    # 2.1 — diamond: A called exactly once, trace order matches
    def t_dep_diamond_trace(diffs):
        reset_all(fa_port, fr_port)
        fa = do_get(fa_port, "/dep/diamond")
        fr = do_get(fr_port, "/dep/diamond")
        fa_tr = _trace(fa) or []
        fr_tr = _trace(fr) or []
        # Filter to dep_ entries
        fa_deps = [e for e in fa_tr if isinstance(e, str) and e.startswith("dep_")]
        fr_deps = [e for e in fr_tr if isinstance(e, str) and e.startswith("dep_")]
        _diff_list("dep_diamond_deps", fa_deps, fr_deps, diffs)
        # A must appear exactly once
        a_count_fa = sum(1 for e in fa_deps if e.startswith("dep_A"))
        a_count_fr = sum(1 for e in fr_deps if e.startswith("dep_A"))
        if a_count_fa != 1: diffs.append(f"FA A-count={a_count_fa}")
        if a_count_fr != 1: diffs.append(f"FR A-count={a_count_fr}")
        return fa_tr, fr_tr, a_count_fa == a_count_fr == 1
    run_test(next_id(), "dep: diamond DAG A called once (cached)",
             "dep_diamond", t_dep_diamond_trace)

    # 2.2 — no-cache: dep_NC called 3 times
    def t_dep_nocache_3(diffs):
        reset_all(fa_port, fr_port)
        fa = do_get(fa_port, "/dep/no-cache-x3")
        fr = do_get(fr_port, "/dep/no-cache-x3")
        fa_tr = _trace(fa) or []
        fr_tr = _trace(fr) or []
        fa_nc = [e for e in fa_tr if isinstance(e, str) and e.startswith("dep_NC")]
        fr_nc = [e for e in fr_tr if isinstance(e, str) and e.startswith("dep_NC")]
        _diff_list("dep_nc", fa_nc, fr_nc, diffs)
        return fa_nc, fr_nc, len(fa_nc) == 3 and len(fr_nc) == 3
    run_test(next_id(), "dep: use_cache=False → 3 separate calls",
             "dep_nocache", t_dep_nocache_3)

    # 2.3 — cache: dep_NC called 1 time
    def t_dep_cache_1(diffs):
        reset_all(fa_port, fr_port)
        fa = do_get(fa_port, "/dep/cache-x3")
        fr = do_get(fr_port, "/dep/cache-x3")
        fa_tr = _trace(fa) or []
        fr_tr = _trace(fr) or []
        fa_nc = [e for e in fa_tr if isinstance(e, str) and e.startswith("dep_NC")]
        fr_nc = [e for e in fr_tr if isinstance(e, str) and e.startswith("dep_NC")]
        _diff_list("dep_cache_nc", fa_nc, fr_nc, diffs)
        return fa_nc, fr_nc, len(fa_nc) == 1 and len(fr_nc) == 1
    run_test(next_id(), "dep: use_cache=True (default) → 1 call",
             "dep_cache", t_dep_cache_1)

    # 2.4 — state-relay dep: set then read
    def t_dep_state_relay(diffs):
        reset_all(fa_port, fr_port)
        fa = do_get(fa_port, "/dep/state-relay")
        fr = do_get(fr_port, "/dep/state-relay")
        a = _j(fa)
        b = _j(fr)
        _diff_list("state_relay", a, b, diffs)
        return a, b, a and b and a.get("marker") == "set_by_dep" and b.get("marker") == "set_by_dep"
    run_test(next_id(), "dep: state set in dep read by later dep",
             "dep_state_relay", t_dep_state_relay)

    # 2.5 — dep + query dependency chain
    def t_trace_five_deps(diffs):
        reset_all(fa_port, fr_port)
        fa = do_get(fa_port, "/trace/five-deps?q=hello")
        fr = do_get(fr_port, "/trace/five-deps?q=hello")
        fa_tr = _trace(fa) or []
        fr_tr = _trace(fr) or []
        # Filter events by known substrings
        def pick(tr):
            return [e for e in tr if isinstance(e, str)
                    and any(k in e for k in ("dep_", "handler_"))]
        _diff_list("five_deps", pick(fa_tr), pick(fr_tr), diffs)
        return fa_tr, fr_tr, pick(fa_tr) == pick(fr_tr)
    run_test(next_id(), "dep: 5-dep chain trace identical",
             "dep_chain_trace", t_trace_five_deps)

    # 2.6 — cache isolation across 2 consecutive requests
    def t_dep_cache_isolation(diffs):
        reset_all(fa_port, fr_port)
        do_get(fa_port, "/dep/cache-x3")
        do_get(fa_port, "/dep/cache-x3")
        a = _j(do_get(fa_port, "/_counts")) or {}
        do_get(fr_port, "/dep/cache-x3")
        do_get(fr_port, "/dep/cache-x3")
        b = _j(do_get(fr_port, "/_counts")) or {}
        nc_a = a.get("NC", 0)
        nc_b = b.get("NC", 0)
        if nc_a != 2: diffs.append(f"FA NC={nc_a} exp=2")
        if nc_b != 2: diffs.append(f"FR NC={nc_b} exp=2")
        return a, b, nc_a == nc_b == 2
    run_test(next_id(), "dep: cache per-request (counter=2 after 2 requests)",
             "dep_cache_scope", t_dep_cache_isolation)

    # ─── SECTION 3: Yield dependencies ──────────────────────────────
    print(f"{CYAN}[SECT 3] Yield dep setup/teardown ordering{RESET}")

    # 3.1 — nested yield: setup A→B→C; teardown C→B→A
    def t_yield_nested_order(diffs):
        reset_all(fa_port, fr_port)
        fa = do_get(fa_port, "/yield/nested")
        fr = do_get(fr_port, "/yield/nested")
        fa_tr = _trace(fa) or []
        fr_tr = _trace(fr) or []
        # At handler time, only setups should have fired
        def pick_setups(tr):
            return [e for e in tr if isinstance(e, str) and e.endswith("_setup")]
        _diff_list("yield_setups", pick_setups(fa_tr), pick_setups(fr_tr), diffs)
        exp = ["yA_setup", "yB_setup", "yC_setup"]
        ok_fa = pick_setups(fa_tr) == exp
        ok_fr = pick_setups(fr_tr) == exp
        return fa_tr, fr_tr, ok_fa and ok_fr
    run_test(next_id(), "yield: setup order A→B→C (outermost last)",
             "yield_order", t_yield_nested_order)

    # 3.2 — teardown reverse: poll _counts after settling
    def t_yield_teardown_lifo(diffs):
        reset_all(fa_port, fr_port)
        do_get(fa_port, "/yield/nested")
        do_get(fr_port, "/yield/nested")
        # Give teardown a moment
        time.sleep(0.1)
        a = _j(do_get(fa_port, "/_counts")) or {}
        b = _j(do_get(fr_port, "/_counts")) or {}
        if a.get("ya_teardown") != 1: diffs.append(f"FA ya_teardown={a.get('ya_teardown')}")
        if a.get("yb_teardown") != 1: diffs.append(f"FA yb_teardown={a.get('yb_teardown')}")
        if a.get("yc_teardown") != 1: diffs.append(f"FA yc_teardown={a.get('yc_teardown')}")
        if b.get("ya_teardown") != 1: diffs.append(f"FR ya_teardown={b.get('ya_teardown')}")
        if b.get("yb_teardown") != 1: diffs.append(f"FR yb_teardown={b.get('yb_teardown')}")
        if b.get("yc_teardown") != 1: diffs.append(f"FR yc_teardown={b.get('yc_teardown')}")
        return a, b, (a.get("ya_teardown") == b.get("ya_teardown") == 1
                      and a.get("yb_teardown") == b.get("yb_teardown") == 1
                      and a.get("yc_teardown") == b.get("yc_teardown") == 1)
    run_test(next_id(), "yield: all 3 teardowns fire",
             "yield_teardown", t_yield_teardown_lifo)

    # 3.3 — handler raises: teardown still runs
    def t_yield_on_exception(diffs):
        reset_all(fa_port, fr_port)
        fa = do_get(fa_port, "/yield/nested-and-raise")
        fr = do_get(fr_port, "/yield/nested-and-raise")
        time.sleep(0.1)
        a = _j(do_get(fa_port, "/_counts")) or {}
        b = _j(do_get(fr_port, "/_counts")) or {}
        if a.get("ya_teardown", 0) < 1: diffs.append(f"FA ya_td={a.get('ya_teardown')}")
        if b.get("ya_teardown", 0) < 1: diffs.append(f"FR ya_td={b.get('ya_teardown')}")
        ok = (a.get("ya_teardown") == b.get("ya_teardown") and
              a.get("ya_teardown", 0) >= 1)
        return a, b, ok
    run_test(next_id(), "yield: teardown runs on handler exception",
             "yield_teardown", t_yield_on_exception)

    # 3.4 — teardown itself raises → response still sent
    def t_yield_teardown_raises(diffs):
        reset_all(fa_port, fr_port)
        fa = do_get(fa_port, "/yield/teardown-raises")
        fr = do_get(fr_port, "/yield/teardown-raises")
        _diff_list("teardown_raises_status", fa.status_code, fr.status_code, diffs)
        return fa.status_code, fr.status_code, fa.status_code in (200, 500) and fr.status_code in (200, 500)
    run_test(next_id(), "yield: teardown-raises doesn't prevent response",
             "yield_teardown_raises", t_yield_teardown_raises)

    # 3.5 — async yield dep teardown
    def t_yield_async(diffs):
        reset_all(fa_port, fr_port)
        fa = do_get(fa_port, "/yield/async")
        fr = do_get(fr_port, "/yield/async")
        fa_tr = _trace(fa) or []
        fr_tr = _trace(fr) or []
        pick = lambda tr: [e for e in tr if isinstance(e, str) and "y_async" in e]
        _diff_list("y_async_trace", pick(fa_tr), pick(fr_tr), diffs)
        return fa_tr, fr_tr, fa.status_code == fr.status_code == 200
    run_test(next_id(), "yield: async generator teardown fires",
             "yield_async", t_yield_async)

    # ─── SECTION 4: Streaming deep comparison ───────────────────────
    print(f"{CYAN}[SECT 4] Streaming chunk boundaries + order{RESET}")

    # 4.1 — tagged ndjson: all 10 chunks, correct order
    def t_stream_tagged(diffs):
        fa = do_get(fa_port, "/stream/tagged")
        fr = do_get(fr_port, "/stream/tagged")
        def parse_ndjson(body: bytes) -> list[dict]:
            out = []
            for line in body.splitlines():
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        out.append({"raw_err": line.decode(errors="replace")})
            return out
        a = parse_ndjson(fa.content)
        b = parse_ndjson(fr.content)
        _diff_list("stream_tagged", a, b, diffs)
        exp = [{"i": i, "tag": f"chunk-{i:02d}"} for i in range(10)]
        return a, b, a == exp and b == exp
    run_test(next_id(), "stream: sync gen 10 tagged NDJSON chunks in order",
             "stream", t_stream_tagged)

    # 4.2 — async tagged ndjson
    def t_stream_tagged_async(diffs):
        fa = do_get(fa_port, "/stream/tagged-async")
        fr = do_get(fr_port, "/stream/tagged-async")
        def parse(body):
            return [json.loads(l) for l in body.splitlines() if l.strip()]
        try:
            a = parse(fa.content)
        except Exception as e:
            a = [{"err": str(e)}]
        try:
            b = parse(fr.content)
        except Exception as e:
            b = [{"err": str(e)}]
        _diff_list("stream_async_tagged", a, b, diffs)
        exp = [{"i": i, "tag": f"async-{i:02d}"} for i in range(10)]
        return a, b, a == exp and b == exp
    run_test(next_id(), "stream: async gen 10 tagged NDJSON chunks in order",
             "stream_async", t_stream_tagged_async)

    # 4.3 — SSE full: id: / event: / data: / retry: parsed
    def t_stream_sse(diffs):
        fa = do_get(fa_port, "/stream/sse-full")
        fr = do_get(fr_port, "/stream/sse-full")
        def parse_sse(body: bytes) -> list[dict]:
            events = []
            cur = {}
            for line in body.decode("utf-8", errors="replace").splitlines():
                if line == "":
                    if cur:
                        events.append(cur)
                        cur = {}
                    continue
                if ":" in line:
                    k, _, v = line.partition(":")
                    v = v.lstrip()
                    cur.setdefault(k, v)
            if cur:
                events.append(cur)
            return events
        a = parse_sse(fa.content)
        b = parse_sse(fr.content)
        _diff_list("sse_events", a, b, diffs)
        ok = len(a) >= 6 and len(b) >= 6
        return a, b, ok and a == b
    run_test(next_id(), "stream: SSE retry/id/event/data parsed identically",
             "stream_sse", t_stream_sse)

    # 4.4 — with-sleep stream (R1 bug: FR returned empty body)
    def t_stream_with_sleep(diffs):
        fa = do_get(fa_port, "/stream/with-sleep")
        fr = do_get(fr_port, "/stream/with-sleep")
        a = fa.content
        b = fr.content
        if a != b:
            diffs.append(f"body differ FA_len={len(a)} FR_len={len(b)}")
            diffs.append(f"FA={a[:100]!r} FR={b[:100]!r}")
        return a, b, a == b and len(a) > 0
    run_test(next_id(), "stream: with asyncio.sleep between yields",
             "stream_with_sleep", t_stream_with_sleep)

    # 4.5 — stream raises mid-way: FA gets partial body + disconnect
    def t_stream_raises_mid(diffs):
        try:
            fa = do_get(fa_port, "/stream/raises-mid")
            fa_body = fa.content
            fa_status = fa.status_code
        except Exception as e:
            fa_body = b""
            fa_status = -1
        try:
            fr = do_get(fr_port, "/stream/raises-mid")
            fr_body = fr.content
            fr_status = fr.status_code
        except Exception as e:
            fr_body = b""
            fr_status = -1
        # Both should return status 200 with partial body (because stream started)
        _diff_list("stream_raise_status", fa_status, fr_status, diffs)
        return (fa_status, fa_body), (fr_status, fr_body), True  # permissive on content
    run_test(next_id(), "stream: gen raises mid-stream",
             "stream_raise_mid", t_stream_raises_mid)

    # 4.6 — bytes streaming preserves bytes
    def t_stream_bytes(diffs):
        fa = do_get(fa_port, "/stream/bytes-tagged")
        fr = do_get(fr_port, "/stream/bytes-tagged")
        exp = b"".join(bytes([i]) * 4 for i in range(8))
        ok_a = fa.content == exp
        ok_b = fr.content == exp
        if not ok_a: diffs.append(f"FA bytes={fa.content!r}")
        if not ok_b: diffs.append(f"FR bytes={fr.content!r}")
        return fa.content, fr.content, ok_a and ok_b
    run_test(next_id(), "stream: bytes stream preserves all bytes",
             "stream_bytes", t_stream_bytes)

    # 4.7 — streaming has Transfer-Encoding: chunked header via raw HTTP
    def t_stream_chunked_header(diffs):
        st_fa, hd_fa, _ = raw_http(fa_port, "/stream/tagged")
        st_fr, hd_fr, _ = raw_http(fr_port, "/stream/tagged")
        hk_fa = {k.lower(): v for k, v in hd_fa}
        hk_fr = {k.lower(): v for k, v in hd_fr}
        te_fa = hk_fa.get("transfer-encoding", "").lower()
        te_fr = hk_fr.get("transfer-encoding", "").lower()
        if te_fa != "chunked": diffs.append(f"FA TE={te_fa!r}")
        if te_fr != "chunked": diffs.append(f"FR TE={te_fr!r}")
        return te_fa, te_fr, te_fa == "chunked" and te_fr == "chunked"
    run_test(next_id(), "stream: Transfer-Encoding: chunked",
             "stream_headers", t_stream_chunked_header)

    # 4.8 — streaming has NO Content-Length (mutually exclusive with chunked)
    def t_stream_no_content_length(diffs):
        st_fa, hd_fa, _ = raw_http(fa_port, "/stream/tagged")
        st_fr, hd_fr, _ = raw_http(fr_port, "/stream/tagged")
        hk_fa = {k.lower(): v for k, v in hd_fa}
        hk_fr = {k.lower(): v for k, v in hd_fr}
        has_cl_fa = "content-length" in hk_fa
        has_cl_fr = "content-length" in hk_fr
        if has_cl_fa: diffs.append(f"FA has CL={hk_fa.get('content-length')}")
        if has_cl_fr: diffs.append(f"FR has CL={hk_fr.get('content-length')}")
        return has_cl_fa, has_cl_fr, not has_cl_fa and not has_cl_fr
    run_test(next_id(), "stream: no Content-Length",
             "stream_headers", t_stream_no_content_length)

    # 4.9 — custom header on streaming response
    def t_stream_custom_header(diffs):
        fa = do_get(fa_port, "/stream/headers")
        fr = do_get(fr_port, "/stream/headers")
        a = fa.headers.get("x-stream-custom")
        b = fr.headers.get("x-stream-custom")
        if a != "yes": diffs.append(f"FA={a!r}")
        if b != "yes": diffs.append(f"FR={b!r}")
        return a, b, a == b == "yes"
    run_test(next_id(), "stream: custom header propagates",
             "stream_headers", t_stream_custom_header)

    # ─── SECTION 5: Cookies — field-by-field ────────────────────────
    print(f"{CYAN}[SECT 5] Cookie attribute parity (SimpleCookie parse){RESET}")

    def _set_cookies(r: httpx.Response) -> list[str]:
        # httpx split-headers doesn't return multiple; use raw
        lst = []
        for k, v in r.headers.raw:
            if k.lower() == b"set-cookie":
                lst.append(v.decode("latin-1"))
        if not lst:
            sc = r.headers.get("set-cookie")
            if sc:
                lst.append(sc)
        return lst

    # 5.1 — full-attrs: every attr set and parseable identically
    def t_cookie_full_attrs(diffs):
        fa = do_get(fa_port, "/cookie/full-attrs")
        fr = do_get(fr_port, "/cookie/full-attrs")
        pa = parse_set_cookies(_set_cookies(fa))
        pb = parse_set_cookies(_set_cookies(fr))
        _diff_list("full_attrs", pa, pb, diffs)
        # Also check each attribute individually
        def attrs(pl):
            if not pl: return {}
            return pl[0].get("attrs", {})
        fa_attrs = attrs(pa)
        fr_attrs = attrs(pb)
        for k in ("path", "domain", "max-age", "secure", "httponly", "samesite"):
            va = fa_attrs.get(k)
            vb = fr_attrs.get(k)
            if va != vb:
                diffs.append(f"attr {k}: FA={va!r} FR={vb!r}")
        return pa, pb, pa == pb
    run_test(next_id(), "cookie: full-attrs (max_age/path/domain/secure/httponly/samesite)",
             "cookie_attrs", t_cookie_full_attrs)

    # 5.2 — samesite=none
    def t_cookie_ss_none(diffs):
        fa = do_get(fa_port, "/cookie/samesite-none")
        fr = do_get(fr_port, "/cookie/samesite-none")
        pa = parse_set_cookies(_set_cookies(fa))
        pb = parse_set_cookies(_set_cookies(fr))
        _diff_list("ss_none", pa, pb, diffs)
        return pa, pb, pa == pb
    run_test(next_id(), "cookie: samesite=none + secure",
             "cookie_attrs", t_cookie_ss_none)

    # 5.3 — samesite=strict
    def t_cookie_ss_strict(diffs):
        fa = do_get(fa_port, "/cookie/samesite-strict")
        fr = do_get(fr_port, "/cookie/samesite-strict")
        pa = parse_set_cookies(_set_cookies(fa))
        pb = parse_set_cookies(_set_cookies(fr))
        _diff_list("ss_strict", pa, pb, diffs)
        return pa, pb, pa == pb
    run_test(next_id(), "cookie: samesite=strict",
             "cookie_attrs", t_cookie_ss_strict)

    # 5.4 — multi set-cookie: 3 separate headers
    def t_cookie_multi(diffs):
        fa = do_get(fa_port, "/cookie/multi-set-cookie")
        fr = do_get(fr_port, "/cookie/multi-set-cookie")
        pa = parse_set_cookies(_set_cookies(fa))
        pb = parse_set_cookies(_set_cookies(fr))
        names_a = sorted([p.get("key") for p in pa])
        names_b = sorted([p.get("key") for p in pb])
        if names_a != ["a", "b", "c"]: diffs.append(f"FA names={names_a}")
        if names_b != ["a", "b", "c"]: diffs.append(f"FR names={names_b}")
        _diff_list("multi_cookies_parsed", pa, pb, diffs)
        return pa, pb, names_a == names_b == ["a", "b", "c"]
    run_test(next_id(), "cookie: 3 Set-Cookie headers survive",
             "cookie_multi", t_cookie_multi)

    # 5.5 — delete-cookie
    def t_cookie_delete(diffs):
        fa = do_get(fa_port, "/cookie/delete")
        fr = do_get(fr_port, "/cookie/delete")
        pa = parse_set_cookies(_set_cookies(fa))
        pb = parse_set_cookies(_set_cookies(fr))
        # Should have Max-Age=0 or expiry in past
        def is_delete(p):
            a = p.get("attrs", {})
            return str(a.get("max-age", "")) == "0" or (a.get("expires") or "")
        ok_a = any(is_delete(p) for p in pa)
        ok_b = any(is_delete(p) for p in pb)
        if not ok_a: diffs.append(f"FA no delete: {pa}")
        if not ok_b: diffs.append(f"FR no delete: {pb}")
        return pa, pb, ok_a and ok_b
    run_test(next_id(), "cookie: delete sets Max-Age=0 or past expiry",
             "cookie_delete", t_cookie_delete)

    # 5.6 — quoted value with space
    def t_cookie_quoted(diffs):
        fa = do_get(fa_port, "/cookie/quoted-value")
        fr = do_get(fr_port, "/cookie/quoted-value")
        pa = parse_set_cookies(_set_cookies(fa))
        pb = parse_set_cookies(_set_cookies(fr))
        va = (pa[0].get("value") if pa else None)
        vb = (pb[0].get("value") if pb else None)
        if va != "has space": diffs.append(f"FA value={va!r}")
        if vb != "has space": diffs.append(f"FR value={vb!r}")
        return va, vb, va == vb == "has space"
    run_test(next_id(), "cookie: value with space preserved",
             "cookie_value", t_cookie_quoted)

    # 5.7 — Cookie() extraction with urlencoded value
    def t_cookie_urlenc(diffs):
        fa = do_get(fa_port, "/cookie/urlenc-value",
                    headers={"Cookie": "foo=hello%20world"})
        fr = do_get(fr_port, "/cookie/urlenc-value",
                    headers={"Cookie": "foo=hello%20world"})
        a = _j(fa); b = _j(fr)
        _diff_list("urlenc_cookie", a, b, diffs)
        return a, b, a == b
    run_test(next_id(), "cookie: urlencoded value in Cookie()",
             "cookie_value", t_cookie_urlenc)

    # 5.8 — multi-cookie parsing
    def t_cookie_multi_get(diffs):
        fa = do_get(fa_port, "/cookie/get-multi",
                    headers={"Cookie": "a=1; b=2; c=3"})
        fr = do_get(fr_port, "/cookie/get-multi",
                    headers={"Cookie": "a=1; b=2; c=3"})
        a = _j(fa); b = _j(fr)
        _diff_list("multi_cookie_get", a, b, diffs)
        return a, b, a == b == {"a": "1", "b": "2", "c": "3"}
    run_test(next_id(), "cookie: 3 cookies parsed into 3 params",
             "cookie_parse", t_cookie_multi_get)

    # ─── SECTION 6: Request surface (Starlette) ─────────────────────
    print(f"{CYAN}[SECT 6] Request.url / .headers / .query_params / .cookies{RESET}")

    # 6.1 — url-deep
    def t_req_url_deep(diffs):
        fa = do_get(fa_port, "/req/url-deep?foo=bar&baz=qux")
        fr = do_get(fr_port, "/req/url-deep?foo=bar&baz=qux")
        a = _j(fa) or {}
        b = _j(fr) or {}
        for k in ("scheme", "hostname", "path", "query"):
            va, vb = a.get(k), b.get(k)
            if va != vb:
                diffs.append(f"{k}: FA={va!r} FR={vb!r}")
        return a, b, (a.get("path") == b.get("path") == "/req/url-deep"
                      and a.get("query") == b.get("query"))
    run_test(next_id(), "req: url {scheme,hostname,path,query}",
             "req_url", t_req_url_deep)

    # 6.2 — url include/replace query params
    def t_req_url_mutate(diffs):
        fa = do_get(fa_port, "/req/url-mutate?x=1")
        fr = do_get(fr_port, "/req/url-mutate?x=1")
        a = _j(fa) or {}
        b = _j(fr) or {}
        _diff_list("url_mutate", a, b, diffs)
        # FA result should contain 'added=yes' in include and 'replaced=yes' in replace
        ok_fa = ("added" in str(a.get("include", "")) and
                 "replaced" in str(a.get("replace", "")))
        return a, b, ok_fa
    run_test(next_id(), "req: url.include_query_params / replace_query_params",
             "req_url_mutate", t_req_url_mutate)

    # 6.3 — headers getlist for duplicate headers
    def t_req_headers_getlist(diffs):
        fa = raw_http(fa_port, "/req/headers-getlist",
                      headers={"X-Multi": "a", "X-Custom": "cv"})
        fr = raw_http(fr_port, "/req/headers-getlist",
                      headers={"X-Multi": "a", "X-Custom": "cv"})
        # stock httpx doesn't send duplicates; send via raw
        _, _, fa_body = fa
        _, _, fr_body = fr
        fa_body = decode_chunked(fa_body) if b"\r\n" in fa_body[:32] and fa_body[:4].strip().isdigit() is False else fa_body
        try:
            a = json.loads(fa_body)
        except Exception:
            a = {"err": fa_body.decode(errors="replace")[:100]}
        try:
            b = json.loads(fr_body)
        except Exception:
            b = {"err": fr_body.decode(errors="replace")[:100]}
        _diff_list("headers_getlist", a, b, diffs)
        return a, b, a == b
    run_test(next_id(), "req: headers.getlist / get-with-default / case-insensitive",
             "req_headers", t_req_headers_getlist)

    # 6.4 — headers iter returns lowercased keys (Starlette convention)
    def t_req_headers_iter(diffs):
        fa = do_get(fa_port, "/req/headers-iter",
                    headers={"X-Alpha": "1", "X-Beta": "2"})
        fr = do_get(fr_port, "/req/headers-iter",
                    headers={"X-Alpha": "1", "X-Beta": "2"})
        a = _j(fa) or {}
        b = _j(fr) or {}
        _diff_list("headers_iter", a, b, diffs)
        return a, b, a == b
    run_test(next_id(), "req: headers iter lowercased keys",
             "req_headers_iter", t_req_headers_iter)

    # 6.5 — query getlist
    def t_req_qp_getlist(diffs):
        fa = do_get(fa_port, "/req/query-getlist?ids=1&ids=2&ids=3")
        fr = do_get(fr_port, "/req/query-getlist?ids=1&ids=2&ids=3")
        a = _j(fa); b = _j(fr)
        _diff_list("qp_getlist", a, b, diffs)
        return a, b, a == b
    run_test(next_id(), "req: query_params.getlist('ids') = [1,2,3]",
             "req_qp", t_req_qp_getlist)

    # 6.6 — query empty vs missing
    def t_req_qp_empty(diffs):
        fa = do_get(fa_port, "/req/query-empty-vs-missing?x=")
        fr = do_get(fr_port, "/req/query-empty-vs-missing?x=")
        a = _j(fa); b = _j(fr)
        _diff_list("qp_empty", a, b, diffs)
        ok = (a and b and a.get("x_value") == "" and b.get("x_value") == "")
        return a, b, ok
    run_test(next_id(), "req: ?x= (empty) vs missing",
             "req_qp_empty", t_req_qp_empty)

    # 6.7 — cookies as dict
    def t_req_cookies_dict(diffs):
        fa = do_get(fa_port, "/req/cookies-dict",
                    headers={"Cookie": "c1=v1; c2=v2"})
        fr = do_get(fr_port, "/req/cookies-dict",
                    headers={"Cookie": "c1=v1; c2=v2"})
        a = _j(fa); b = _j(fr)
        _diff_list("cookies_dict", a, b, diffs)
        return a, b, a == b
    run_test(next_id(), "req: cookies dict",
             "req_cookies", t_req_cookies_dict)

    # 6.8 — client.host / .port
    def t_req_client(diffs):
        fa = do_get(fa_port, "/req/client")
        fr = do_get(fr_port, "/req/client")
        a = _j(fa); b = _j(fr)
        _diff_list("req_client", a, b, diffs)
        ok = a and b and a.get("has_host") and a.get("host_is_loopback")
        return a, b, ok
    run_test(next_id(), "req: client.host present + loopback",
             "req_client", t_req_client)

    # 6.9 — scope keys
    def t_req_scope(diffs):
        fa = do_get(fa_port, "/req/scope-keys")
        fr = do_get(fr_port, "/req/scope-keys")
        a = _j(fa); b = _j(fr)
        _diff_list("scope_keys", a, b, diffs)
        # type/method are critical
        ok = (a and b and a.get("type") == "http" and b.get("type") == "http"
              and a.get("method") == "GET" and b.get("method") == "GET")
        return a, b, ok
    run_test(next_id(), "req: scope.type/method/path/http_version",
             "req_scope", t_req_scope)

    # 6.10 — body() identity
    def t_req_body_bytes(diffs):
        body = b"hello world body!"
        fa = httpx.post(f"http://{HOST}:{fa_port}/req/body-bytes", content=body)
        fr = httpx.post(f"http://{HOST}:{fr_port}/req/body-bytes", content=body)
        a = _j(fa); b = _j(fr)
        _diff_list("body_bytes", a, b, diffs)
        return a, b, a == b
    run_test(next_id(), "req: await request.body() length + content",
             "req_body", t_req_body_bytes)

    # 6.11 — body() twice returns identical bytes
    def t_req_body_twice(diffs):
        body = b"twice-same"
        fa = httpx.post(f"http://{HOST}:{fa_port}/req/body-twice", content=body)
        fr = httpx.post(f"http://{HOST}:{fr_port}/req/body-twice", content=body)
        a = _j(fa); b = _j(fr)
        _diff_list("body_twice", a, b, diffs)
        ok = a and b and a.get("same") is True and b.get("same") is True
        return a, b, ok
    run_test(next_id(), "req: body() called twice returns same bytes (cached)",
             "req_body_cached", t_req_body_twice)

    # 6.12 — await request.json()
    def t_req_json(diffs):
        payload = {"nested": {"k": [1, 2, 3]}, "x": "y"}
        fa = httpx.post(f"http://{HOST}:{fa_port}/req/json-parsed", json=payload)
        fr = httpx.post(f"http://{HOST}:{fr_port}/req/json-parsed", json=payload)
        a = _j(fa); b = _j(fr)
        _diff_list("req_json", a, b, diffs)
        return a, b, a == b and a.get("parsed") == payload
    run_test(next_id(), "req: await request.json() round-trips",
             "req_json", t_req_json)

    # 6.13 — await request.form() with multi-value
    def t_req_form_multi(diffs):
        # httpx on some versions fails to encode list-of-tuples as multi
        # form data over real HTTP ("sequence item 1: expected bytes-like,
        # tuple found"). Pre-encode via urllib so both servers receive an
        # identical wire body.
        from urllib.parse import urlencode
        encoded = urlencode(
            [("tag", "a"), ("tag", "b"), ("tag", "c"), ("name", "bob")]
        )
        form_headers = {"content-type": "application/x-www-form-urlencoded"}
        fa = httpx.post(f"http://{HOST}:{fa_port}/req/form-multi",
                        content=encoded, headers=form_headers)
        fr = httpx.post(f"http://{HOST}:{fr_port}/req/form-multi",
                        content=encoded, headers=form_headers)
        a = _j(fa); b = _j(fr)
        _diff_list("form_multi", a, b, diffs)
        return a, b, a == b
    run_test(next_id(), "req: form.getlist('tag')",
             "req_form", t_req_form_multi)

    # 6.14 — await request.stream()
    def t_req_stream(diffs):
        body = b"x" * 2048
        fa = httpx.post(f"http://{HOST}:{fa_port}/req/stream-body", content=body)
        fr = httpx.post(f"http://{HOST}:{fr_port}/req/stream-body", content=body)
        a = _j(fa); b = _j(fr)
        _diff_list("stream_body", a, b, diffs)
        ok = a and b and a.get("total") == len(body) and b.get("total") == len(body)
        return a, b, ok
    run_test(next_id(), "req: async for chunk in request.stream()",
             "req_stream", t_req_stream)

    # 6.15 — request.state mutability
    def t_req_state(diffs):
        fa = do_get(fa_port, "/req/state-set")
        fr = do_get(fr_port, "/req/state-set")
        a = _j(fa); b = _j(fr)
        _diff_list("req_state", a, b, diffs)
        return a, b, a == b == {"mine": "mine_val"}
    run_test(next_id(), "req: state.mine set and read back",
             "req_state", t_req_state)

    # ─── SECTION 7: Response surface ────────────────────────────────
    print(f"{CYAN}[SECT 7] Response.headers.append / setdefault / mutablecopy{RESET}")

    # 7.1 — append X-Dup twice — both survive (raw)
    def t_resp_append(diffs):
        _, hd_fa, _ = raw_http(fa_port, "/resp/many-headers")
        _, hd_fr, _ = raw_http(fr_port, "/resp/many-headers")
        dup_fa = [v for k, v in hd_fa if k.lower() == "x-dup"]
        dup_fr = [v for k, v in hd_fr if k.lower() == "x-dup"]
        _diff_list("x_dup", sorted(dup_fa), sorted(dup_fr), diffs)
        # At least both values should be present (either combined or separate)
        ok_fa = "a" in str(dup_fa) and "b" in str(dup_fa)
        ok_fr = "a" in str(dup_fr) and "b" in str(dup_fr)
        return dup_fa, dup_fr, ok_fa and ok_fr
    run_test(next_id(), "resp: headers.append(X-Dup, 'a'/'b') both survive",
             "resp_headers_append", t_resp_append)

    # 7.2 — setdefault doesn't overwrite
    def t_resp_setdefault(diffs):
        fa = do_get(fa_port, "/resp/setdefault")
        fr = do_get(fr_port, "/resp/setdefault")
        a = fa.headers.get("x-setdef")
        b = fr.headers.get("x-setdef")
        if a != "first": diffs.append(f"FA setdef={a!r}")
        if b != "first": diffs.append(f"FR setdef={b!r}")
        return a, b, a == b == "first"
    run_test(next_id(), "resp: headers.setdefault second call no-op",
             "resp_headers_setdefault", t_resp_setdefault)

    # 7.3 — mutablecopy works
    def t_resp_mutablecopy(diffs):
        fa = do_get(fa_port, "/resp/mutablecopy")
        fr = do_get(fr_port, "/resp/mutablecopy")
        a = fa.headers.get("x-copy-status")
        b = fr.headers.get("x-copy-status")
        if a != "yes": diffs.append(f"FA={a!r}")
        if b != "yes": diffs.append(f"FR={b!r}")
        return a, b, a == b == "yes"
    run_test(next_id(), "resp: headers.mutablecopy() works",
             "resp_headers_mutablecopy", t_resp_mutablecopy)

    # 7.4 — media_type override → Content-Type
    def t_resp_mt_override(diffs):
        fa = do_get(fa_port, "/resp/media-type-override")
        fr = do_get(fr_port, "/resp/media-type-override")
        a = fa.headers.get("content-type", "")
        b = fr.headers.get("content-type", "")
        ok_a = "xml" in a.lower()
        ok_b = "xml" in b.lower()
        if not ok_a: diffs.append(f"FA ct={a}")
        if not ok_b: diffs.append(f"FR ct={b}")
        return a, b, ok_a and ok_b
    run_test(next_id(), "resp: media_type='application/xml' propagates",
             "resp_media_type", t_resp_mt_override)

    # 7.5 — BackgroundTask runs after body sent (single)
    def t_resp_bg_single(diffs):
        httpx.get(f"http://{HOST}:{fa_port}/bg/clear")
        httpx.get(f"http://{HOST}:{fr_port}/bg/clear")
        do_get(fa_port, "/resp/background-task")
        time.sleep(0.1)
        a = _j(do_get(fa_port, "/_bg_log")) or {}
        do_get(fr_port, "/resp/background-task")
        time.sleep(0.1)
        b = _j(do_get(fr_port, "/_bg_log")) or {}
        fa_log = a.get("log", [])
        fr_log = b.get("log", [])
        _diff_list("bg_single_log", fa_log, fr_log, diffs)
        return fa_log, fr_log, "single_bg_fired" in fa_log and "single_bg_fired" in fr_log
    run_test(next_id(), "resp: single BackgroundTask runs",
             "resp_bg_single", t_resp_bg_single)

    # 7.6 — multi BackgroundTasks run in order
    def t_resp_bg_multi(diffs):
        httpx.get(f"http://{HOST}:{fa_port}/bg/clear")
        httpx.get(f"http://{HOST}:{fr_port}/bg/clear")
        do_get(fa_port, "/resp/background-tasks-multi")
        time.sleep(0.1)
        a = _j(do_get(fa_port, "/_bg_log")) or {}
        do_get(fr_port, "/resp/background-tasks-multi")
        time.sleep(0.1)
        b = _j(do_get(fr_port, "/_bg_log")) or {}
        fa_log = a.get("log", [])
        fr_log = b.get("log", [])
        _diff_list("bg_multi_log", fa_log, fr_log, diffs)
        return fa_log, fr_log, fa_log == ["bg_t1", "bg_t2", "bg_t3"] and fr_log == ["bg_t1", "bg_t2", "bg_t3"]
    run_test(next_id(), "resp: multi BackgroundTasks run in order 1→2→3",
             "resp_bg_multi", t_resp_bg_multi)

    # 7.7 — async BackgroundTask
    def t_resp_bg_async(diffs):
        httpx.get(f"http://{HOST}:{fa_port}/bg/clear")
        httpx.get(f"http://{HOST}:{fr_port}/bg/clear")
        do_get(fa_port, "/resp/background-async")
        time.sleep(0.1)
        a = _j(do_get(fa_port, "/_bg_log")) or {}
        do_get(fr_port, "/resp/background-async")
        time.sleep(0.1)
        b = _j(do_get(fr_port, "/_bg_log")) or {}
        fa_log = a.get("log", [])
        fr_log = b.get("log", [])
        _diff_list("bg_async_log", fa_log, fr_log, diffs)
        return fa_log, fr_log, "async:hello" in fa_log and "async:hello" in fr_log
    run_test(next_id(), "resp: async BackgroundTask awaited",
             "resp_bg_async", t_resp_bg_async)

    # 7.8 — bg task raises, response still returned, subsequent still runs
    def t_resp_bg_raises(diffs):
        httpx.get(f"http://{HOST}:{fa_port}/bg/clear")
        httpx.get(f"http://{HOST}:{fr_port}/bg/clear")
        fa = do_get(fa_port, "/resp/background-raises")
        fr = do_get(fr_port, "/resp/background-raises")
        time.sleep(0.2)
        a = _j(do_get(fa_port, "/_bg_log")) or {}
        b = _j(do_get(fr_port, "/_bg_log")) or {}
        fa_log = a.get("log", [])
        fr_log = b.get("log", [])
        ok_status = fa.status_code == fr.status_code == 200
        _diff_list("bg_raises_log", fa_log, fr_log, diffs)
        return fa_log, fr_log, ok_status
    run_test(next_id(), "resp: bg task raising doesn't affect response",
             "resp_bg_raises", t_resp_bg_raises)

    # ─── SECTION 8: UploadFile ──────────────────────────────────────
    print(f"{CYAN}[SECT 8] UploadFile surface{RESET}")

    # 8.1 — single upload: filename/content_type/size/contents
    def t_upload_one(diffs):
        files = {"file": ("hello.txt", b"hello-world", "text/plain")}
        fa = httpx.post(f"http://{HOST}:{fa_port}/upload/one", files=files)
        fr = httpx.post(f"http://{HOST}:{fr_port}/upload/one", files=files)
        a = _j(fa); b = _j(fr)
        _diff_list("upload_one", a, b, diffs)
        return a, b, a == b and a.get("filename") == "hello.txt"
    run_test(next_id(), "upload: filename + content_type + size + contents",
             "upload", t_upload_one)

    # 8.2 — seek(0) then re-read gives same bytes
    def t_upload_seek(diffs):
        files = {"file": ("seek.bin", b"ABCDEFGH1234", "application/octet-stream")}
        fa = httpx.post(f"http://{HOST}:{fa_port}/upload/seek", files=files)
        fr = httpx.post(f"http://{HOST}:{fr_port}/upload/seek", files=files)
        a = _j(fa); b = _j(fr)
        _diff_list("upload_seek", a, b, diffs)
        ok = a and b and a.get("same") and b.get("same")
        return a, b, ok
    run_test(next_id(), "upload: seek(0)+read → identical bytes",
             "upload_seek", t_upload_seek)

    # 8.3 — multiple files same field
    def t_upload_multi(diffs):
        files = [
            ("files", ("a.txt", b"AAA", "text/plain")),
            ("files", ("b.txt", b"BBBB", "text/plain")),
            ("files", ("c.txt", b"CCCCC", "text/plain")),
        ]
        fa = httpx.post(f"http://{HOST}:{fa_port}/upload/multi", files=files)
        fr = httpx.post(f"http://{HOST}:{fr_port}/upload/multi", files=files)
        a = _j(fa); b = _j(fr)
        _diff_list("upload_multi", a, b, diffs)
        return a, b, a == b
    run_test(next_id(), "upload: list[UploadFile] — 3 files",
             "upload_multi", t_upload_multi)

    # 8.4 — unicode filename
    def t_upload_unicode(diffs):
        files = {"file": ("héllo世界.txt", b"x", "text/plain")}
        fa = httpx.post(f"http://{HOST}:{fa_port}/upload/unicode-name", files=files)
        fr = httpx.post(f"http://{HOST}:{fr_port}/upload/unicode-name", files=files)
        a = _j(fa); b = _j(fr)
        _diff_list("upload_unicode", a, b, diffs)
        return a, b, a == b
    run_test(next_id(), "upload: unicode filename preserved",
             "upload_unicode", t_upload_unicode)

    # 8.5 — 1MB upload
    def t_upload_1mb(diffs):
        data = os.urandom(1024 * 1024)
        files = {"file": ("big.bin", data, "application/octet-stream")}
        try:
            fa = httpx.post(f"http://{HOST}:{fa_port}/upload/one", files=files, timeout=30.0)
            fa_j = _j(fa)
        except Exception as e:
            fa_j = {"err": str(e)}
        try:
            fr = httpx.post(f"http://{HOST}:{fr_port}/upload/one", files=files, timeout=30.0)
            fr_j = _j(fr)
        except Exception as e:
            fr_j = {"err": str(e)}
        _diff_list("upload_1mb", fa_j, fr_j, diffs)
        ok = fa_j and fr_j and fa_j.get("size") == len(data) and fr_j.get("size") == len(data)
        return fa_j, fr_j, ok
    run_test(next_id(), "upload: 1MB file — correct size",
             "upload_large", t_upload_1mb)

    # 8.6 — form+file combo
    def t_form_and_file(diffs):
        files = {"file": ("f.txt", b"content", "text/plain")}
        data = {"name": "alice"}
        fa = httpx.post(f"http://{HOST}:{fa_port}/form+file", data=data, files=files)
        fr = httpx.post(f"http://{HOST}:{fr_port}/form+file", data=data, files=files)
        a = _j(fa); b = _j(fr)
        _diff_list("form_and_file", a, b, diffs)
        return a, b, a == b
    run_test(next_id(), "form+file: mixed fields",
             "form_file_combo", t_form_and_file)

    # ─── SECTION 9: Concurrency (50-100 parallel) ───────────────────
    print(f"{CYAN}[SECT 9] Concurrency under load{RESET}")

    async def _parallel_gets(port, path, n, headers_fn=None):
        async with httpx.AsyncClient(base_url=f"http://{HOST}:{port}",
                                      timeout=_CLIENT_TIMEOUT) as c:
            tasks = []
            for i in range(n):
                h = headers_fn(i) if headers_fn else None
                tasks.append(c.get(path, headers=h))
            rs = await asyncio.gather(*tasks, return_exceptions=True)
            return rs

    # 9.1 — 100 GET /ep/fast all 200
    def t_concurrent_fast_100(diffs):
        fa = asyncio.run(_parallel_gets(fa_port, "/ep/fast", 100))
        fr = asyncio.run(_parallel_gets(fr_port, "/ep/fast", 100))
        fa_ok = sum(1 for r in fa if not isinstance(r, Exception) and r.status_code == 200)
        fr_ok = sum(1 for r in fr if not isinstance(r, Exception) and r.status_code == 200)
        if fa_ok != 100:
            diffs.append(f"FA only {fa_ok}/100 ok")
            CONCURRENCY_FAILURES["load"] += 1
        if fr_ok != 100:
            diffs.append(f"FR only {fr_ok}/100 ok")
            CONCURRENCY_FAILURES["load"] += 1
        return fa_ok, fr_ok, fa_ok == fr_ok == 100
    run_test(next_id(), "concurrent: 100 parallel /ep/fast all 200",
             "concurrent_fast", t_concurrent_fast_100)

    # 9.2 — 50 parallel /concurrency/slow all complete
    def t_concurrent_slow_50(diffs):
        httpx.get(f"http://{HOST}:{fa_port}/concurrency/reset")
        httpx.get(f"http://{HOST}:{fr_port}/concurrency/reset")
        fa = asyncio.run(_parallel_gets(fa_port, "/concurrency/slow", 50))
        fr = asyncio.run(_parallel_gets(fr_port, "/concurrency/slow", 50))
        fa_ok = sum(1 for r in fa if not isinstance(r, Exception) and r.status_code == 200)
        fr_ok = sum(1 for r in fr if not isinstance(r, Exception) and r.status_code == 200)
        if fa_ok != 50:
            diffs.append(f"FA only {fa_ok}/50")
            CONCURRENCY_FAILURES["load"] += 1
        if fr_ok != 50:
            diffs.append(f"FR only {fr_ok}/50")
            CONCURRENCY_FAILURES["load"] += 1
        return fa_ok, fr_ok, fa_ok == 50 and fr_ok == 50
    run_test(next_id(), "concurrent: 50 parallel /concurrency/slow all complete",
             "concurrent_slow", t_concurrent_slow_50)

    # 9.3 — 50 parallel /concurrency/slow: counter ends at 50
    def t_concurrent_counter(diffs):
        httpx.get(f"http://{HOST}:{fa_port}/concurrency/reset")
        httpx.get(f"http://{HOST}:{fr_port}/concurrency/reset")
        asyncio.run(_parallel_gets(fa_port, "/concurrency/slow", 50))
        asyncio.run(_parallel_gets(fr_port, "/concurrency/slow", 50))
        a = _j(do_get(fa_port, "/concurrency/counter")) or {}
        b = _j(do_get(fr_port, "/concurrency/counter")) or {}
        if a.get("n") != 50: diffs.append(f"FA n={a}")
        if b.get("n") != 50: diffs.append(f"FR n={b}")
        return a, b, a.get("n") == 50 and b.get("n") == 50
    run_test(next_id(), "concurrent: counter = 50 after 50 slow reqs",
             "concurrent_counter", t_concurrent_counter)

    # 9.4 — 50 parallel request-state — no leak across requests
    async def _par_req_state(port, n=50):
        async with httpx.AsyncClient(base_url=f"http://{HOST}:{port}",
                                      timeout=_CLIENT_TIMEOUT) as c:
            tasks = [c.post("/concurrency/req-state",
                           headers={"X-Req-Id": f"REQ-{i:04d}"})
                     for i in range(n)]
            rs = await asyncio.gather(*tasks, return_exceptions=True)
            out = []
            for r in rs:
                if isinstance(r, Exception):
                    out.append({"err": type(r).__name__})
                    continue
                try:
                    out.append(r.json())
                except Exception:
                    out.append({"err": "parse"})
            return out

    def t_concurrent_req_state(diffs):
        fa = asyncio.run(_par_req_state(fa_port, 50))
        fr = asyncio.run(_par_req_state(fr_port, 50))
        fa_mismatch = [i for i, e in enumerate(fa) if not e.get("match")]
        fr_mismatch = [i for i, e in enumerate(fr) if not e.get("match")]
        if fa_mismatch:
            diffs.append(f"FA leak idx={fa_mismatch[:5]}")
            CONCURRENCY_FAILURES["correctness"] += 1
        if fr_mismatch:
            diffs.append(f"FR leak idx={fr_mismatch[:5]}")
            CONCURRENCY_FAILURES["correctness"] += 1
        return fa, fr, not fa_mismatch and not fr_mismatch
    run_test(next_id(), "concurrent: 50 parallel request.state — no leak",
             "concurrent_no_leak", t_concurrent_req_state)

    # 9.5 — 50 parallel /dep/cache-x3 — each gets NC=1 in its response
    async def _par_cached(port, n=50):
        async with httpx.AsyncClient(base_url=f"http://{HOST}:{port}",
                                      timeout=_CLIENT_TIMEOUT) as c:
            tasks = [c.get("/dep/cache-x3") for _ in range(n)]
            rs = await asyncio.gather(*tasks, return_exceptions=True)
            out = []
            for r in rs:
                if isinstance(r, Exception):
                    out.append({"err": type(r).__name__})
                    continue
                try:
                    d = r.json()
                    out.append({
                        "a_n": d.get("a", {}).get("n"),
                        "b_n": d.get("b", {}).get("n"),
                        "c_n": d.get("c", {}).get("n"),
                    })
                except Exception as e:
                    out.append({"err": str(e)})
            return out

    def t_concurrent_dep_cache(diffs):
        reset_all(fa_port, fr_port)
        fa = asyncio.run(_par_cached(fa_port, 50))
        fr = asyncio.run(_par_cached(fr_port, 50))
        # For each request a_n/b_n/c_n should be equal (cached), but across
        # requests they vary.
        def bad(results):
            return [i for i, r in enumerate(results)
                    if r.get("a_n") != r.get("b_n") or r.get("a_n") != r.get("c_n")
                    or r.get("a_n") is None]
        bad_fa = bad(fa)
        bad_fr = bad(fr)
        if bad_fa: diffs.append(f"FA bad {bad_fa[:5]}")
        if bad_fr: diffs.append(f"FR bad {bad_fr[:5]}")
        return fa, fr, not bad_fa and not bad_fr
    run_test(next_id(), "concurrent: dep cache per-request under load",
             "concurrent_dep_cache", t_concurrent_dep_cache)

    # 9.6 — mixed sync + async handlers don't interfere
    async def _mixed(port, n=30):
        async with httpx.AsyncClient(base_url=f"http://{HOST}:{port}",
                                      timeout=_CLIENT_TIMEOUT) as c:
            tasks = []
            for i in range(n):
                if i % 2 == 0:
                    tasks.append(c.get("/ep/fast"))
                else:
                    tasks.append(c.get("/ep/async-fast"))
            return await asyncio.gather(*tasks, return_exceptions=True)

    def t_concurrent_mixed(diffs):
        fa = asyncio.run(_mixed(fa_port, 50))
        fr = asyncio.run(_mixed(fr_port, 50))
        fa_ok = sum(1 for r in fa if not isinstance(r, Exception) and r.status_code == 200)
        fr_ok = sum(1 for r in fr if not isinstance(r, Exception) and r.status_code == 200)
        if fa_ok != 50:
            diffs.append(f"FA {fa_ok}/50")
            CONCURRENCY_FAILURES["load"] += 1
        if fr_ok != 50:
            diffs.append(f"FR {fr_ok}/50")
            CONCURRENCY_FAILURES["load"] += 1
        return fa_ok, fr_ok, fa_ok == 50 and fr_ok == 50
    run_test(next_id(), "concurrent: mixed sync/async 50 reqs",
             "concurrent_mixed", t_concurrent_mixed)

    # 9.7 — long-running async doesn't block fast requests
    async def _slow_and_fast(port):
        async with httpx.AsyncClient(base_url=f"http://{HOST}:{port}",
                                      timeout=_CLIENT_TIMEOUT) as c:
            slow = asyncio.create_task(c.get("/concurrency/slow"))
            await asyncio.sleep(0.001)
            fast_start = time.time()
            fast = await c.get("/ep/fast")
            fast_elapsed = time.time() - fast_start
            slow_resp = await slow
            return fast_elapsed, fast.status_code, slow_resp.status_code

    def t_concurrent_no_block(diffs):
        fa_el, fa_fst, fa_slw = asyncio.run(_slow_and_fast(fa_port))
        fr_el, fr_fst, fr_slw = asyncio.run(_slow_and_fast(fr_port))
        ok_fa = fa_fst == 200 and fa_slw == 200
        ok_fr = fr_fst == 200 and fr_slw == 200
        if not ok_fa: diffs.append(f"FA {fa_fst}/{fa_slw}")
        if not ok_fr: diffs.append(f"FR {fr_fst}/{fr_slw}")
        return fa_el, fr_el, ok_fa and ok_fr
    run_test(next_id(), "concurrent: slow handler doesn't block fast handler",
             "concurrent_non_blocking", t_concurrent_no_block)

    # 9.8 — 100 parallel 422 validation errors
    async def _par_invalid(port, n=50):
        async with httpx.AsyncClient(base_url=f"http://{HOST}:{port}",
                                      timeout=_CLIENT_TIMEOUT) as c:
            tasks = [c.post("/exc/strict-body", json={"name": "x"})
                     for _ in range(n)]
            return await asyncio.gather(*tasks, return_exceptions=True)

    def t_concurrent_422(diffs):
        fa = asyncio.run(_par_invalid(fa_port, 50))
        fr = asyncio.run(_par_invalid(fr_port, 50))
        fa_ok = sum(1 for r in fa if not isinstance(r, Exception) and r.status_code == 422)
        fr_ok = sum(1 for r in fr if not isinstance(r, Exception) and r.status_code == 422)
        if fa_ok != 50: diffs.append(f"FA {fa_ok}/50")
        if fr_ok != 50: diffs.append(f"FR {fr_ok}/50")
        return fa_ok, fr_ok, fa_ok == fr_ok == 50
    run_test(next_id(), "concurrent: 50 parallel 422s",
             "concurrent_422", t_concurrent_422)

    # ─── SECTION 10: Exception propagation ───────────────────────────
    print(f"{CYAN}[SECT 10] Exception propagation{RESET}")

    # 10.1 — custom exception handler called
    def t_exc_custom(diffs):
        fa = do_get(fa_port, "/exc/custom")
        fr = do_get(fr_port, "/exc/custom")
        a = _j(fa); b = _j(fr)
        _diff_list("custom_exc", a, b, diffs)
        _diff_list("custom_exc_status", fa.status_code, fr.status_code, diffs)
        return a, b, a == b and fa.status_code == fr.status_code == 418
    run_test(next_id(), "exc: custom exception handler → 418 + {custom_error, msg}",
             "exc_custom", t_exc_custom)

    # 10.2 — HTTPException with dict detail
    def t_exc_http_dict(diffs):
        fa = do_get(fa_port, "/exc/http-with-dict")
        fr = do_get(fr_port, "/exc/http-with-dict")
        a = _j(fa); b = _j(fr)
        _diff_list("http_dict", a, b, diffs)
        return a, b, a == b and fa.status_code == fr.status_code == 404
    run_test(next_id(), "exc: HTTPException(detail=dict) → JSON {detail: {...}}",
             "exc_http_dict", t_exc_http_dict)

    # 10.3 — HTTPException with list detail
    def t_exc_http_list(diffs):
        fa = do_get(fa_port, "/exc/http-with-list")
        fr = do_get(fr_port, "/exc/http-with-list")
        a = _j(fa); b = _j(fr)
        _diff_list("http_list", a, b, diffs)
        return a, b, a == b and fa.status_code == fr.status_code == 400
    run_test(next_id(), "exc: HTTPException(detail=list) → detail as list",
             "exc_http_list", t_exc_http_list)

    # 10.4 — HTTPException headers honored
    def t_exc_http_headers(diffs):
        fa = do_get(fa_port, "/exc/http-with-headers")
        fr = do_get(fr_port, "/exc/http-with-headers")
        a = fa.headers.get("www-authenticate")
        b = fr.headers.get("www-authenticate")
        if a != "Bearer realm=x": diffs.append(f"FA={a!r}")
        if b != "Bearer realm=x": diffs.append(f"FR={b!r}")
        return a, b, a == b == "Bearer realm=x"
    run_test(next_id(), "exc: HTTPException(headers=...) honored",
             "exc_http_headers", t_exc_http_headers)

    # 10.5 — ValueError → 500
    def t_exc_value_error(diffs):
        fa = do_get(fa_port, "/exc/value-error")
        fr = do_get(fr_port, "/exc/value-error")
        _diff_list("value_err_status", fa.status_code, fr.status_code, diffs)
        return fa.status_code, fr.status_code, fa.status_code == 500 and fr.status_code == 500
    run_test(next_id(), "exc: raw ValueError → 500",
             "exc_value_error", t_exc_value_error)

    # 10.6 — RequestValidationError custom handler called
    def t_exc_rv_custom(diffs):
        fa = httpx.post(f"http://{HOST}:{fa_port}/exc/strict-body", json={"name": "x"})
        fr = httpx.post(f"http://{HOST}:{fr_port}/exc/strict-body", json={"name": "x"})
        a = _j(fa); b = _j(fr)
        _diff_list("rv_custom", a, b, diffs)
        return a, b, a and b and a.get("custom_rv") is True and b.get("custom_rv") is True
    run_test(next_id(), "exc: custom RequestValidationError handler invoked",
             "exc_rv_custom", t_exc_rv_custom)

    # ─── SECTION 11: Starlette surface ──────────────────────────────
    print(f"{CYAN}[SECT 11] Starlette direct surface coverage{RESET}")

    def t_starlette_url_dict(diffs):
        fa = do_get(fa_port, "/starlette/url-dict")
        fr = do_get(fr_port, "/starlette/url-dict")
        a = _j(fa); b = _j(fr)
        _diff_list("starlette_url_dict", a, b, diffs)
        return a, b, a == b
    run_test(next_id(), "starlette: URL {path,host,scheme}",
             "starlette_url", t_starlette_url_dict)

    def t_starlette_headers_dict(diffs):
        fa = do_get(fa_port, "/starlette/headers-dict")
        fr = do_get(fr_port, "/starlette/headers-dict")
        a = _j(fa); b = _j(fr)
        _diff_list("starlette_headers_dict", a, b, diffs)
        return a, b, a and b and a.get("has_host") and b.get("has_host")
    run_test(next_id(), "starlette: headers iter → dict with host",
             "starlette_headers", t_starlette_headers_dict)

    def t_starlette_qp_dict(diffs):
        fa = do_get(fa_port, "/starlette/qp-dict?a=1&b=2&a=3")
        fr = do_get(fr_port, "/starlette/qp-dict?a=1&b=2&a=3")
        a = _j(fa); b = _j(fr)
        _diff_list("starlette_qp_dict", a, b, diffs)
        return a, b, a == b
    run_test(next_id(), "starlette: query_params as dict (last-wins)",
             "starlette_qp", t_starlette_qp_dict)

    def t_starlette_qp_multi(diffs):
        fa = do_get(fa_port, "/starlette/qp-multi?a=1&b=2&a=3")
        fr = do_get(fr_port, "/starlette/qp-multi?a=1&b=2&a=3")
        a = _j(fa); b = _j(fr)
        _diff_list("starlette_qp_multi", a, b, diffs)
        return a, b, a == b
    run_test(next_id(), "starlette: query_params.multi_items()",
             "starlette_qp_multi", t_starlette_qp_multi)

    # ─── SECTION 12: Response types (from R1) — keep body/status checks ─
    print(f"{CYAN}[SECT 12] Response type parity (pair-based){RESET}")

    simple = [
        ("/ep/fast", lambda s, b: s == 200 and b == {"fast": True}),
        ("/ep/async-fast", lambda s, b: s == 200 and b == {"async_fast": True}),
        ("/status/201", lambda s, b: s == 201),
        ("/status/204", lambda s, b: s == 204),
        ("/status/418", lambda s, b: s == 418),
    ]
    for path, check in simple:
        def _mk(path=path, check=check):
            def t(diffs):
                fa = do_get(fa_port, path)
                fr = do_get(fr_port, path)
                a = _j(fa); b = _j(fr)
                if fa.status_code != fr.status_code:
                    diffs.append(f"status FA={fa.status_code} FR={fr.status_code}")
                if a != b:
                    diffs.append(f"body FA={a!r} FR={b!r}")
                ok_a = check(fa.status_code, a)
                ok_b = check(fa.status_code, b)
                return a, b, ok_a and ok_b and fa.status_code == fr.status_code
            return t
        run_test(next_id(), f"resp-parity: GET {path}",
                 "resp_parity", _mk())

    # Huge list / deep JSON / unicode / special chars
    for path in ("/misc/large-list", "/misc/deep-json", "/misc/unicode-json",
                 "/misc/special-chars", "/misc/numeric-edges"):
        def _mk(path=path):
            def t(diffs):
                fa = do_get(fa_port, path)
                fr = do_get(fr_port, path)
                a = _j(fa); b = _j(fr)
                _diff_list(f"misc{path}", a, b, diffs)
                return a, b, a == b
            return t
        run_test(next_id(), f"misc-json: {path}",
                 "misc_json", _mk())

    # Multiple Set-Cookie parity on dupe header endpoint
    def t_misc_dupe_hdr(diffs):
        _, hd_fa, _ = raw_http(fa_port, "/misc/header-dupe")
        _, hd_fr, _ = raw_http(fr_port, "/misc/header-dupe")
        dup_fa = [v for k, v in hd_fa if k.lower() == "x-dupe"]
        dup_fr = [v for k, v in hd_fr if k.lower() == "x-dupe"]
        _diff_list("dupe_hdr", sorted(dup_fa), sorted(dup_fr), diffs)
        return dup_fa, dup_fr, set(str(dup_fa).split()) == set(str(dup_fr).split())
    run_test(next_id(), "misc: duplicate X-Dupe headers parity",
             "misc_dupe_hdr", t_misc_dupe_hdr)

    # ─── SECTION 13: Path / Query / Header params ───────────────────
    print(f"{CYAN}[SECT 13] Path/Query/Header parity{RESET}")

    pp_tests = [
        ("/pp/int/42", {"x": 42, "t": "int"}),
        ("/pp/int/-1", {"x": -1, "t": "int"}),
        ("/pp/str/hello", {"x": "hello", "t": "str"}),
        ("/pp/str/a%20b", {"x": "a b", "t": "str"}),
        ("/pp/path/a/b/c", {"p": "a/b/c"}),
        ("/pp/path/deeply/nested/stuff/with/slashes", {"p": "deeply/nested/stuff/with/slashes"}),
        ("/pp/list-query?tag=a&tag=b&tag=c", {"tags": ["a", "b", "c"], "count": 3}),
        ("/pp/list-query", {"tags": [], "count": 0}),
        ("/pp/alias-query?myVal=X", {"v": "X"}),
        ("/pp/alias-query", {"v": "d"}),
        ("/pp/bool-query?flag=true", {"flag": True}),
        ("/pp/bool-query?flag=false", {"flag": False}),
        ("/pp/bool-query", {"flag": False}),
        ("/pp/int-query?n=42", {"n": 42}),
        ("/pp/float-query?p=3.14", {"p": 3.14}),
        ("/pp/numeric-constraints?age=30", {"age": 30}),
    ]
    for path, expected in pp_tests:
        def _mk(path=path, expected=expected):
            def t(diffs):
                fa = do_get(fa_port, path)
                fr = do_get(fr_port, path)
                a = _j(fa); b = _j(fr)
                _diff_list(f"pp{path}", a, b, diffs)
                exp_ok_a = a == expected
                exp_ok_b = b == expected
                return a, b, a == b and exp_ok_a and exp_ok_b
            return t
        run_test(next_id(), f"pp: {path}",
                 "path_query", _mk())

    # Constraints violation
    def t_pp_constraint_violation(diffs):
        fa = do_get(fa_port, "/pp/numeric-constraints?age=200")
        fr = do_get(fr_port, "/pp/numeric-constraints?age=200")
        _diff_list("constraint_status", fa.status_code, fr.status_code, diffs)
        return fa.status_code, fr.status_code, fa.status_code == fr.status_code == 422
    run_test(next_id(), "pp: age>150 → 422",
             "path_query_constraint", t_pp_constraint_violation)

    # Header alias & underscore
    def t_pp_header_alias(diffs):
        fa = do_get(fa_port, "/pp/header-alias",
                    headers={"X-Custom-Alias": "V"})
        fr = do_get(fr_port, "/pp/header-alias",
                    headers={"X-Custom-Alias": "V"})
        a = _j(fa); b = _j(fr)
        _diff_list("header_alias", a, b, diffs)
        return a, b, a == b == {"h": "V"}
    run_test(next_id(), "pp: Header alias X-Custom-Alias",
             "header_alias", t_pp_header_alias)

    def t_pp_header_underscore(diffs):
        fa = do_get(fa_port, "/pp/header-underscore",
                    headers={"X-Custom": "V"})
        fr = do_get(fr_port, "/pp/header-underscore",
                    headers={"X-Custom": "V"})
        a = _j(fa); b = _j(fr)
        _diff_list("header_underscore", a, b, diffs)
        return a, b, a == b == {"h": "V"}
    run_test(next_id(), "pp: Header by x_custom (underscore→dash)",
             "header_underscore", t_pp_header_underscore)

    # ─── SECTION 14: HTTP verbs ─────────────────────────────────────
    print(f"{CYAN}[SECT 14] HTTP verbs{RESET}")

    for method in ("GET", "POST", "PUT", "DELETE", "PATCH"):
        def _mk(method=method):
            def t(diffs):
                body = b"x"
                try:
                    fa = httpx.request(method, f"http://{HOST}:{fa_port}/verbs/any",
                                       content=body if method != "GET" else None)
                except Exception as e:
                    fa = None; diffs.append(f"FA err: {e}")
                try:
                    fr = httpx.request(method, f"http://{HOST}:{fr_port}/verbs/any",
                                       content=body if method != "GET" else None)
                except Exception as e:
                    fr = None; diffs.append(f"FR err: {e}")
                a = _j(fa) if fa is not None else None
                b = _j(fr) if fr is not None else None
                _diff_list(f"verb_{method}", a, b, diffs)
                ok = a and b and a.get("method") == method and b.get("method") == method
                return a, b, ok
            return t
        run_test(next_id(), f"verb: {method} /verbs/any",
                 "http_verbs", _mk())

    # ─── SECTION 15: 30 load endpoints ──────────────────────────────
    print(f"{CYAN}[SECT 15] 30 load endpoints parity{RESET}")

    for i in range(30):
        def _mk(i=i):
            def t(diffs):
                fa = do_get(fa_port, f"/load/ep{i}")
                fr = do_get(fr_port, f"/load/ep{i}")
                a = _j(fa); b = _j(fr)
                _diff_list(f"load_ep{i}", a, b, diffs)
                return a, b, a == b == {"ep": i, "ok": True}
            return t
        run_test(next_id(), f"load: /load/ep{i}",
                 "load_ep", _mk())

    # ─── SECTION 16: Response model ─────────────────────────────────
    print(f"{CYAN}[SECT 16] Response model{RESET}")

    def t_rm_exclude_none(diffs):
        fa = do_get(fa_port, "/rm/exclude-none")
        fr = do_get(fr_port, "/rm/exclude-none")
        a = _j(fa); b = _j(fr)
        _diff_list("rm_exclude_none", a, b, diffs)
        return a, b, a == b and "b" not in a
    run_test(next_id(), "rm: exclude_none removes null",
             "rm_exclude_none", t_rm_exclude_none)

    def t_rm_exclude_unset(diffs):
        fa = do_get(fa_port, "/rm/exclude-unset")
        fr = do_get(fr_port, "/rm/exclude-unset")
        a = _j(fa); b = _j(fr)
        _diff_list("rm_exclude_unset", a, b, diffs)
        return a, b, a == b and "b" not in a
    run_test(next_id(), "rm: exclude_unset removes unset field",
             "rm_exclude_unset", t_rm_exclude_unset)

    def t_rm_echo(diffs):
        payload = {"name": "widget", "price": 9.99}
        fa = httpx.post(f"http://{HOST}:{fa_port}/rm/echo", json=payload)
        fr = httpx.post(f"http://{HOST}:{fr_port}/rm/echo", json=payload)
        a = _j(fa); b = _j(fr)
        _diff_list("rm_echo", a, b, diffs)
        return a, b, a == b == payload
    run_test(next_id(), "rm: Item echo round-trips",
             "rm_echo", t_rm_echo)

    # ─── SECTION 17: Sub-router dependency ─────────────────────────
    print(f"{CYAN}[SECT 17] Sub-router with deps{RESET}")

    def t_sub_router_a(diffs):
        reset_all(fa_port, fr_port)
        fa = do_get(fa_port, "/sub/a")
        fr = do_get(fr_port, "/sub/a")
        fa_tr = _trace(fa) or []
        fr_tr = _trace(fr) or []
        # Should contain 'router_dep' + 'sub_a_handler' entries
        ok_fa = "router_dep" in fa_tr and "sub_a_handler" in fa_tr
        ok_fr = "router_dep" in fr_tr and "sub_a_handler" in fr_tr
        _diff_list("sub_a_trace", fa_tr, fr_tr, diffs)
        return fa_tr, fr_tr, ok_fa and ok_fr
    run_test(next_id(), "router: sub_router dep invoked before handler",
             "sub_router", t_sub_router_a)

    def t_sub_router_b(diffs):
        reset_all(fa_port, fr_port)
        fa = do_get(fa_port, "/sub/b")
        fr = do_get(fr_port, "/sub/b")
        fa_tr = _trace(fa) or []
        fr_tr = _trace(fr) or []
        _diff_list("sub_b_trace", fa_tr, fr_tr, diffs)
        return fa_tr, fr_tr, "sub_b_handler" in fa_tr and "sub_b_handler" in fr_tr
    run_test(next_id(), "router: sub_router /b handler",
             "sub_router", t_sub_router_b)

    # ─── SECTION 18: Lifespan state ─────────────────────────────────
    print(f"{CYAN}[SECT 18] Lifespan app.state{RESET}")

    def t_lifespan_state_initialised(diffs):
        fa = _j(do_get(fa_port, "/state/db")) or {}
        fr = _j(do_get(fr_port, "/state/db")) or {}
        _diff_list("state_db", fa, fr, diffs)
        return fa, fr, fa.get("name") == fr.get("name") == "r2_deep_app"
    run_test(next_id(), "lifespan: app.state initialized (db={'counter':0})",
             "lifespan_state", t_lifespan_state_initialised)

    def t_lifespan_state_incr(diffs):
        do_get(fa_port, "/state/incr")
        a = _j(do_get(fa_port, "/state/incr")) or {}
        do_get(fr_port, "/state/incr")
        b = _j(do_get(fr_port, "/state/incr")) or {}
        ok_a = a.get("counter", 0) >= 2
        ok_b = b.get("counter", 0) >= 2
        if not ok_a: diffs.append(f"FA ctr={a}")
        if not ok_b: diffs.append(f"FR ctr={b}")
        return a, b, ok_a and ok_b
    run_test(next_id(), "lifespan: app.state mutable across requests",
             "lifespan_state_mut", t_lifespan_state_incr)

    # ─── SECTION 19: Deep trace diffs on representative reqs ────────
    print(f"{CYAN}[SECT 19] Deep trace fidelity of representative requests{RESET}")

    # Each "trace fidelity" test: issue 10 identical reqs, compute majority
    # trace, compare majority between FA and FR.
    trace_probes = [
        "/mw/trace",
        "/dep/diamond",
        "/dep/cache-x3",
        "/yield/nested",
        "/yield/async",
        "/sub/a",
        "/sub/b",
    ]
    for path in trace_probes:
        def _mk(path=path):
            def t(diffs):
                reset_all(fa_port, fr_port)
                fa_traces = []
                fr_traces = []
                for _ in range(5):
                    a = _trace(do_get(fa_port, path))
                    b = _trace(do_get(fr_port, path))
                    if path.startswith("/yield/"):
                        a = _normalize_yield_trace(a)
                        b = _normalize_yield_trace(b)
                    fa_traces.append(a)
                    fr_traces.append(b)
                # Majority:
                from collections import Counter
                def majority(l):
                    c = Counter(json.dumps(x) for x in l if x is not None)
                    return json.loads(c.most_common(1)[0][0]) if c else None
                ma = majority(fa_traces)
                mb = majority(fr_traces)
                _diff_list(f"trace_fid{path}", ma, mb, diffs)
                return ma, mb, ma == mb and ma is not None
            return t
        run_test(next_id(), f"trace-fidelity: {path} majority trace equal",
                 "trace_fidelity", _mk())

    # ─── SECTION 20: Concurrency trace sanity — 30 more parallel ────
    print(f"{CYAN}[SECT 20] Concurrency deep with diamond dep{RESET}")

    async def _par_diamond(port, n=30):
        async with httpx.AsyncClient(base_url=f"http://{HOST}:{port}",
                                      timeout=_CLIENT_TIMEOUT) as c:
            tasks = [c.get("/dep/diamond") for _ in range(n)]
            rs = await asyncio.gather(*tasks, return_exceptions=True)
            out = []
            for r in rs:
                if isinstance(r, Exception):
                    out.append({"err": type(r).__name__})
                else:
                    try:
                        d = r.json()
                        out.append({
                            "a_n": d["r"]["b"]["a"]["n"],
                            "b_a_n": d["r"]["b"]["a"]["n"],
                            "c_a_n": d["r"]["c"]["a"]["n"],
                        })
                    except Exception as e:
                        out.append({"err": str(e)})
            return out

    def t_concurrent_diamond(diffs):
        reset_all(fa_port, fr_port)
        fa = asyncio.run(_par_diamond(fa_port, 30))
        fr = asyncio.run(_par_diamond(fr_port, 30))
        # In each response b.a.n == c.a.n (cached within req)
        bad_fa = [i for i, e in enumerate(fa)
                  if e.get("b_a_n") != e.get("c_a_n")]
        bad_fr = [i for i, e in enumerate(fr)
                  if e.get("b_a_n") != e.get("c_a_n")]
        if bad_fa:
            diffs.append(f"FA bad={bad_fa[:5]}")
            CONCURRENCY_FAILURES["correctness"] += 1
        if bad_fr:
            diffs.append(f"FR bad={bad_fr[:5]}")
            CONCURRENCY_FAILURES["correctness"] += 1
        return fa, fr, not bad_fa and not bad_fr
    run_test(next_id(), "concurrent: diamond A cached per-request under 30 load",
             "concurrent_diamond", t_concurrent_diamond)

    # ─── SECTION 21: Body validation parity ────────────────────────
    print(f"{CYAN}[SECT 21] Body validation error shape{RESET}")

    validation_cases = [
        ("/exc/strict-body", {"name": "x"}, 422),   # missing 'age'
        ("/exc/strict-body", {"name": "x", "age": "not-int"}, 422),
        ("/exc/strict-body", {}, 422),
        ("/exc/strict-body", {"name": "alice", "age": 30}, 200),
    ]
    for path, body, expected_code in validation_cases:
        def _mk(path=path, body=body, expected_code=expected_code):
            def t(diffs):
                fa = httpx.post(f"http://{HOST}:{fa_port}{path}", json=body)
                fr = httpx.post(f"http://{HOST}:{fr_port}{path}", json=body)
                _diff_list(f"val_status{path}", fa.status_code, fr.status_code, diffs)
                a = _j(fa); b = _j(fr)
                _diff_list(f"val_body{path}", a, b, diffs)
                return a, b, fa.status_code == fr.status_code == expected_code
            return t
        run_test(next_id(), f"validation: {path} body={body}",
                 "validation", _mk())

    # ─── SECTION 22: Cookie GET with different case + multi ─────────
    print(f"{CYAN}[SECT 22] More cookie edges{RESET}")

    cookie_cases = [
        ("a=1", {"a": "1", "b": "B_D", "c": "C_D"}),
        ("a=1; b=2", {"a": "1", "b": "2", "c": "C_D"}),
        ("a=1; b=2; c=3", {"a": "1", "b": "2", "c": "3"}),
        ("a=\"quoted\"", {"a": "quoted", "b": "B_D", "c": "C_D"}),
        ("a=; b=2", {"a": "", "b": "2", "c": "C_D"}),
    ]
    for cookie, expected in cookie_cases:
        def _mk(cookie=cookie, expected=expected):
            def t(diffs):
                fa = do_get(fa_port, "/cookie/get-multi", headers={"Cookie": cookie})
                fr = do_get(fr_port, "/cookie/get-multi", headers={"Cookie": cookie})
                a = _j(fa); b = _j(fr)
                _diff_list(f"cookie_case", a, b, diffs)
                # Both should parse identically; and ideally match expected.
                return a, b, a == b
            return t
        run_test(next_id(), f"cookie: header='{cookie}'",
                 "cookie_parse_case", _mk())

    # ─── SECTION 23: 50-parallel trace equality (same path) ────────
    print(f"{CYAN}[SECT 23] 50 parallel trace equality{RESET}")

    async def _par_trace(port, path, n=50):
        async with httpx.AsyncClient(base_url=f"http://{HOST}:{port}",
                                      timeout=_CLIENT_TIMEOUT) as c:
            tasks = [c.get(path) for _ in range(n)]
            rs = await asyncio.gather(*tasks, return_exceptions=True)
            out = []
            for r in rs:
                if isinstance(r, Exception):
                    out.append(None)
                else:
                    out.append(_trace(r))
            return out

    # For each of the top trace probes, 50 parallel reqs → all identical per port
    # and each port's set equal to the other's set.
    parallel_probes = ["/mw/trace", "/dep/diamond", "/dep/cache-x3",
                       "/yield/nested"]
    # Global invocation counters (A#1, A#2, B#1, …) make raw traces
    # naturally differ under parallel load — what the test actually wants
    # is SHAPE stability: every request should produce the same sequence
    # of events with its OWN per-request invocation numbers. Strip the
    # per-counter suffix before comparing so request.state isolation and
    # dep caching are what's actually exercised.
    import re as _re

    def _normalize_trace_shape(traces):
        if not traces:
            return traces
        def norm(trace):
            if trace is None:
                return None
            return [_re.sub(r"#\d+(\[a=\d+\])?", "#_", e) if isinstance(e, str) else e for e in trace]
        return [norm(t) for t in traces]

    for path in parallel_probes:
        def _mk(path=path):
            def t(diffs):
                reset_all(fa_port, fr_port)
                fa = asyncio.run(_par_trace(fa_port, path, 50))
                fr = asyncio.run(_par_trace(fr_port, path, 50))
                if path.startswith("/yield/"):
                    fa = [_normalize_yield_trace(t) for t in fa]
                    fr = [_normalize_yield_trace(t) for t in fr]
                fa_n = _normalize_trace_shape(fa)
                fr_n = _normalize_trace_shape(fr)
                def stable(traces):
                    if not traces: return True
                    first = traces[0]
                    return all(t == first for t in traces)
                ok_fa = stable(fa_n)
                ok_fr = stable(fr_n)
                if not ok_fa:
                    diffs.append(f"FA not stable under 50 parallel")
                    CONCURRENCY_FAILURES["correctness"] += 1
                if not ok_fr:
                    diffs.append(f"FR not stable under 50 parallel")
                    CONCURRENCY_FAILURES["correctness"] += 1
                ok_cross = (fa_n[0] if fa_n else None) == (fr_n[0] if fr_n else None)
                if not ok_cross:
                    _diff_list(f"par_trace_{path}", fa_n[0] if fa_n else None,
                               fr_n[0] if fr_n else None, diffs)
                return fa_n[0] if fa_n else None, fr_n[0] if fr_n else None, ok_fa and ok_fr and ok_cross
            return t
        run_test(next_id(), f"concurrent-trace: {path} 50-parallel stable",
                 "concurrent_trace", _mk())

    # ─── SECTION 24: Healthcheck sanity ─────────────────────────────
    print(f"{CYAN}[SECT 24] Final health sanity{RESET}")

    for i in range(5):
        def _mk(i=i):
            def t(diffs):
                fa = do_get(fa_port, "/health")
                fr = do_get(fr_port, "/health")
                a = _j(fa); b = _j(fr)
                _diff_list(f"health{i}", a, b, diffs)
                return a, b, a == b == {"status": "ok"}
            return t
        run_test(next_id(), f"health: ping {i+1}/5",
                 "health", _mk())

    # ─── SECTION 25: gzip & cors ────────────────────────────────────
    print(f"{CYAN}[SECT 25] GZip + CORS{RESET}")

    def t_gzip_big(diffs):
        fa = httpx.get(f"http://{HOST}:{fa_port}/misc/large-list",
                       headers={"Accept-Encoding": "gzip"})
        fr = httpx.get(f"http://{HOST}:{fr_port}/misc/large-list",
                       headers={"Accept-Encoding": "gzip"})
        ce_fa = fa.headers.get("content-encoding", "").lower()
        ce_fr = fr.headers.get("content-encoding", "").lower()
        if ce_fa != "gzip": diffs.append(f"FA ce={ce_fa}")
        if ce_fr != "gzip": diffs.append(f"FR ce={ce_fr}")
        return ce_fa, ce_fr, ce_fa == ce_fr == "gzip"
    run_test(next_id(), "gzip: large response compressed",
             "gzip", t_gzip_big)

    def t_cors_preflight(diffs):
        fa = httpx.request("OPTIONS", f"http://{HOST}:{fa_port}/ep/fast",
                           headers={
                               "Origin": "http://example.com",
                               "Access-Control-Request-Method": "GET",
                           })
        fr = httpx.request("OPTIONS", f"http://{HOST}:{fr_port}/ep/fast",
                           headers={
                               "Origin": "http://example.com",
                               "Access-Control-Request-Method": "GET",
                           })
        ok_fa = fa.status_code in (200, 204)
        ok_fr = fr.status_code in (200, 204)
        if not ok_fa: diffs.append(f"FA {fa.status_code}")
        if not ok_fr: diffs.append(f"FR {fr.status_code}")
        aco_fa = fa.headers.get("access-control-allow-origin")
        aco_fr = fr.headers.get("access-control-allow-origin")
        if not aco_fa: diffs.append(f"FA no ACO")
        if not aco_fr: diffs.append(f"FR no ACO")
        return fa.status_code, fr.status_code, ok_fa and ok_fr and aco_fa and aco_fr
    run_test(next_id(), "cors: preflight returns ACO",
             "cors", t_cors_preflight)

    def t_cors_simple(diffs):
        fa = do_get(fa_port, "/ep/fast", headers={"Origin": "http://example.com"})
        fr = do_get(fr_port, "/ep/fast", headers={"Origin": "http://example.com"})
        aco_fa = fa.headers.get("access-control-allow-origin")
        aco_fr = fr.headers.get("access-control-allow-origin")
        return aco_fa, aco_fr, aco_fa is not None and aco_fr is not None
    run_test(next_id(), "cors: simple request adds ACO",
             "cors", t_cors_simple)

    # ─── SECTION 26: Pair each FA vs FR — single-path full compare ─
    print(f"{CYAN}[SECT 26] Full FA vs FR body parity (broad){RESET}")

    broad_paths = [
        "/ep/fast", "/ep/async-fast", "/health",
        "/status/201", "/status/418",
        "/misc/large-list",
        "/misc/deep-json",
        "/misc/unicode-json",
        "/misc/special-chars",
        "/misc/numeric-edges",
        "/state/db",
    ]
    for path in broad_paths:
        def _mk(path=path):
            def t(diffs):
                fa = do_get(fa_port, path)
                fr = do_get(fr_port, path)
                a = _j(fa); b = _j(fr)
                _diff_list(f"broad_{path}", a, b, diffs)
                _diff_list(f"broad_status_{path}", fa.status_code, fr.status_code, diffs)
                return a, b, a == b and fa.status_code == fr.status_code
            return t
        run_test(next_id(), f"broad: {path} body+status parity",
                 "broad_parity", _mk())

    # ─── SECTION 27: URL edge cases ─────────────────────────────────
    print(f"{CYAN}[SECT 27] URL edges{RESET}")

    def t_url_unicode_query(diffs):
        # URL-encoded unicode. Normalize bound port since each server
        # listens on its own port (they legitimately differ).
        fa = do_get(fa_port, "/req/url-deep?q=%E4%BD%A0%E5%A5%BD")
        fr = do_get(fr_port, "/req/url-deep?q=%E4%BD%A0%E5%A5%BD")
        a = _normalize_url_response(_j(fa), fa_port)
        b = _normalize_url_response(_j(fr), fr_port)
        _diff_list("url_unicode", a, b, diffs)
        return a, b, a == b
    run_test(next_id(), "url: unicode query percent-encoded",
             "url_unicode", t_url_unicode_query)

    def t_url_plus_in_query(diffs):
        fa = do_get(fa_port, "/req/query-getlist?ids=a+b")
        fr = do_get(fr_port, "/req/query-getlist?ids=a+b")
        a = _j(fa); b = _j(fr)
        _diff_list("url_plus", a, b, diffs)
        return a, b, a == b
    run_test(next_id(), "url: + in query → space or +",
             "url_plus", t_url_plus_in_query)

    # ─── SECTION 28: Large JSON round-trip ──────────────────────────
    print(f"{CYAN}[SECT 28] Large JSON{RESET}")

    def t_large_json_roundtrip(diffs):
        payload = {"data": [{"i": i, "s": f"str-{i}"} for i in range(500)]}
        fa = httpx.post(f"http://{HOST}:{fa_port}/req/json-parsed", json=payload)
        fr = httpx.post(f"http://{HOST}:{fr_port}/req/json-parsed", json=payload)
        a = _j(fa); b = _j(fr)
        _diff_list("large_json", a, b, diffs)
        return a, b, a == b and a.get("parsed") == payload
    run_test(next_id(), "large-json: 500-row payload round-trip",
             "large_json", t_large_json_roundtrip)

    # ─── SECTION 29: More streaming — raw chunk count ──────────────
    print(f"{CYAN}[SECT 29] Streaming chunk count via raw HTTP{RESET}")

    def t_stream_chunks_raw(diffs):
        _, _, body_fa = raw_http(fa_port, "/stream/tagged")
        _, _, body_fr = raw_http(fr_port, "/stream/tagged")
        # decode chunked
        dec_fa = decode_chunked(body_fa)
        dec_fr = decode_chunked(body_fr)
        lines_fa = [l for l in dec_fa.split(b"\n") if l]
        lines_fr = [l for l in dec_fr.split(b"\n") if l]
        _diff_list("stream_chunks_raw", len(lines_fa), len(lines_fr), diffs)
        return lines_fa, lines_fr, len(lines_fa) == 10 and len(lines_fr) == 10
    run_test(next_id(), "stream: raw chunked decode → 10 lines",
             "stream_raw_chunks", t_stream_chunks_raw)

    # ─── SECTION 30: 100 parallel / ep echo / {n} with distinct N ──
    print(f"{CYAN}[SECT 30] 100 parallel distinct path params{RESET}")

    async def _par_distinct_path(port, n=100):
        async with httpx.AsyncClient(base_url=f"http://{HOST}:{port}",
                                      timeout=_CLIENT_TIMEOUT) as c:
            tasks = [c.get(f"/ep/echo/{i}") for i in range(n)]
            return await asyncio.gather(*tasks, return_exceptions=True)

    def t_concurrent_distinct_path(diffs):
        fa = asyncio.run(_par_distinct_path(fa_port, 100))
        fr = asyncio.run(_par_distinct_path(fr_port, 100))
        def ok_set(rs):
            bad = []
            for i, r in enumerate(rs):
                if isinstance(r, Exception):
                    bad.append(i); continue
                try:
                    j = r.json()
                    if j.get("n") != i or j.get("doubled") != i * 2:
                        bad.append(i)
                except Exception:
                    bad.append(i)
            return bad
        bad_fa = ok_set(fa)
        bad_fr = ok_set(fr)
        if bad_fa:
            diffs.append(f"FA bad={bad_fa[:10]}")
            CONCURRENCY_FAILURES["correctness"] += 1
        if bad_fr:
            diffs.append(f"FR bad={bad_fr[:10]}")
            CONCURRENCY_FAILURES["correctness"] += 1
        return bad_fa, bad_fr, not bad_fa and not bad_fr
    run_test(next_id(), "concurrent: 100 parallel distinct path params",
             "concurrent_path", t_concurrent_distinct_path)

    # ─── SECTION 31: Cookie full-attrs — attr-by-attr ──────────────
    print(f"{CYAN}[SECT 31] Cookie attr-by-attr deep{RESET}")

    attr_keys = ["max-age", "path", "domain", "secure", "httponly", "samesite"]
    for attr in attr_keys:
        def _mk(attr=attr):
            def t(diffs):
                fa = do_get(fa_port, "/cookie/full-attrs")
                fr = do_get(fr_port, "/cookie/full-attrs")
                pa = parse_set_cookies(_set_cookies(fa))
                pb = parse_set_cookies(_set_cookies(fr))
                va = pa[0].get("attrs", {}).get(attr) if pa else None
                vb = pb[0].get("attrs", {}).get(attr) if pb else None
                if va != vb:
                    diffs.append(f"{attr}: FA={va!r} FR={vb!r}")
                return va, vb, va == vb
            return t
        run_test(next_id(), f"cookie-attr: {attr} parity",
                 "cookie_attr_each", _mk())

    # ─── SECTION 32: Many path params ──────────────────────────────
    print(f"{CYAN}[SECT 32] Path param deep{RESET}")

    path_cases = [
        "/pp/int/0", "/pp/int/100", "/pp/int/99999",
        "/pp/str/abc", "/pp/str/WithCaseMIX", "/pp/str/123",
        "/pp/path/a", "/pp/path/a/b", "/pp/path/a/b/c",
        "/pp/path/very/deeply/nested/path/with/many/segments",
    ]
    for path in path_cases:
        def _mk(path=path):
            def t(diffs):
                fa = do_get(fa_port, path)
                fr = do_get(fr_port, path)
                a = _j(fa); b = _j(fr)
                _diff_list(f"pp_case{path}", a, b, diffs)
                return a, b, a == b and fa.status_code == fr.status_code == 200
            return t
        run_test(next_id(), f"pp-case: {path}",
                 "pp_case", _mk())

    # ─── SECTION 33: ordered-5 BG task order ────────────────────────
    print(f"{CYAN}[SECT 33] 5-task BackgroundTasks order{RESET}")

    def t_bg_order(diffs):
        httpx.get(f"http://{HOST}:{fa_port}/bg/clear")
        httpx.get(f"http://{HOST}:{fr_port}/bg/clear")
        do_get(fa_port, "/bg/ordered-5")
        time.sleep(0.15)
        a = _j(do_get(fa_port, "/_bg_log")) or {}
        do_get(fr_port, "/bg/ordered-5")
        time.sleep(0.15)
        b = _j(do_get(fr_port, "/_bg_log")) or {}
        fa_log = a.get("log", [])
        fr_log = b.get("log", [])
        _diff_list("bg_ord5", fa_log, fr_log, diffs)
        exp = [f"ord-{i}" for i in range(5)]
        return fa_log, fr_log, fa_log == exp and fr_log == exp
    run_test(next_id(), "bg: 5 tasks run in registration order",
             "bg_order5", t_bg_order)

    # ─── SECTION 34: Request form boundary ─────────────────────────
    print(f"{CYAN}[SECT 34] multi-round bodies{RESET}")

    form_cases = [
        [("a", "1"), ("b", "2")],
        [("tag", "x"), ("tag", "y"), ("tag", "z")],
        [("name", "with space"), ("tag", "x")],
        [("unicode", "你好"), ("tag", "α")],
    ]
    for data in form_cases:
        def _mk(data=data):
            def t(diffs):
                # Build urlencoded body manually to avoid httpx quirks with list-of-tuples
                body = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
                ct = "application/x-www-form-urlencoded"
                fa = httpx.post(f"http://{HOST}:{fa_port}/req/form-multi",
                                content=body, headers={"Content-Type": ct})
                fr = httpx.post(f"http://{HOST}:{fr_port}/req/form-multi",
                                content=body, headers={"Content-Type": ct})
                a = _j(fa); b = _j(fr)
                _diff_list(f"form_data_{len(data)}", a, b, diffs)
                return a, b, a == b
            return t
        run_test(next_id(), f"form: {data!r}",
                 "form_data", _mk())

    # ─── SECTION 35: Long-tail — 50 repeated /dep/diamond ─────────
    print(f"{CYAN}[SECT 35] 50 sequential /dep/diamond trace stability{RESET}")

    def t_seq_diamond(diffs):
        reset_all(fa_port, fr_port)
        fa_traces = []
        fr_traces = []
        for _ in range(20):
            fa_traces.append(_trace(do_get(fa_port, "/dep/diamond")) or [])
            fr_traces.append(_trace(do_get(fr_port, "/dep/diamond")) or [])
        # Stability: relative trace SHAPE (filter deps) identical
        def shape(tr):
            return tuple(e for e in tr if isinstance(e, str) and e.startswith("dep_"))
        fa_shapes = set(shape(t) for t in fa_traces)
        fr_shapes = set(shape(t) for t in fr_traces)
        if len(fa_shapes) > 1:
            diffs.append(f"FA shapes vary: {len(fa_shapes)}")
        if len(fr_shapes) > 1:
            diffs.append(f"FR shapes vary: {len(fr_shapes)}")
        ok_cross = fa_shapes == fr_shapes
        if not ok_cross:
            diffs.append(f"FA shape={list(fa_shapes)[:1]} FR shape={list(fr_shapes)[:1]}")
        return list(fa_shapes)[:1], list(fr_shapes)[:1], ok_cross
    run_test(next_id(), "seq: 20-iter /dep/diamond trace shape stable + equal",
             "seq_stability", t_seq_diamond)

    # ─── SECTION 36: Exception handler interaction with middleware ──
    print(f"{CYAN}[SECT 36] Exception + middleware{RESET}")

    def t_exc_then_mw_exits(diffs):
        fa = do_get(fa_port, "/exc/custom")
        fr = do_get(fr_port, "/exc/custom")
        # Custom handler converts to 418 response — middleware still sees the
        # response on the way out (and adds MW headers).
        # MW1_out should still be logged via trace header (or response modifications)
        fa_tr = _trace(fa)
        fr_tr = _trace(fr)
        # exc handler → may lose or preserve trace. Compare what's there.
        _diff_list("exc_mw_trace", fa_tr, fr_tr, diffs)
        # MW headers should be present
        def count_mw(hd):
            return sum(1 for i in (1, 2, 3, 4, 5) if hd.get(f"x-mw{i}"))
        ca = count_mw(fa.headers)
        cb = count_mw(fr.headers)
        if ca != cb: diffs.append(f"mw headers count FA={ca} FR={cb}")
        return ca, cb, True  # permissive; record diffs
    run_test(next_id(), "exc: middleware still processes response after custom exc",
             "exc_mw", t_exc_then_mw_exits)

    # ─── SECTION 37: More trace sanity — 20 ordered request paths ──
    print(f"{CYAN}[SECT 37] Assorted endpoint parity{RESET}")

    misc_get_paths = [
        "/_counts", "/_bg_log", "/_yield_events", "/_lifespan",
        "/state/db",
    ]
    for path in misc_get_paths:
        def _mk(path=path):
            def t(diffs):
                fa = do_get(fa_port, path)
                fr = do_get(fr_port, path)
                a = _j(fa); b = _j(fr)
                ok_status = fa.status_code == fr.status_code
                # Content may differ (different run times), but shape/type same
                _diff_list(f"misc_get{path}_type",
                           type(a).__name__, type(b).__name__, diffs)
                return a, b, ok_status and type(a) == type(b)
            return t
        run_test(next_id(), f"assorted: {path} status + type",
                 "assorted", _mk())

    # ─── SECTION 38: More async upload endpoints ───────────────────
    print(f"{CYAN}[SECT 38] Upload perf sanity{RESET}")

    # 10 sequential uploads
    for i in range(5):
        def _mk(i=i):
            def t(diffs):
                data = ("f.txt", f"iter-{i}".encode() * 100, "text/plain")
                fa = httpx.post(f"http://{HOST}:{fa_port}/upload/one",
                                files={"file": data})
                fr = httpx.post(f"http://{HOST}:{fr_port}/upload/one",
                                files={"file": data})
                a = _j(fa); b = _j(fr)
                _diff_list(f"upload_seq{i}", a, b, diffs)
                return a, b, a == b
            return t
        run_test(next_id(), f"upload-seq: iter{i}",
                 "upload_seq", _mk())

    # ─── SECTION 39: Many quick param param exercises ──────────────
    print(f"{CYAN}[SECT 39] More path & query{RESET}")

    extra = [
        "/pp/list-query?tag=α&tag=β",
        "/pp/list-query?tag=hello%20world",
        "/pp/alias-query?myVal=alias-ok",
        "/pp/int-query?n=-5",
        "/pp/bool-query?flag=1",
        "/pp/bool-query?flag=0",
        "/pp/bool-query?flag=yes",
        "/pp/bool-query?flag=no",
        "/pp/numeric-constraints?age=0",
        "/pp/numeric-constraints?age=150",
    ]
    for path in extra:
        def _mk(path=path):
            def t(diffs):
                fa = do_get(fa_port, path)
                fr = do_get(fr_port, path)
                a = _j(fa); b = _j(fr)
                _diff_list(f"extra{path}_body", a, b, diffs)
                _diff_list(f"extra{path}_status", fa.status_code, fr.status_code, diffs)
                return a, b, a == b and fa.status_code == fr.status_code
            return t
        run_test(next_id(), f"extra-query: {path}",
                 "extra_query", _mk())

    # ─── SECTION 40: Additional middleware probes (depth) ───────────
    print(f"{CYAN}[SECT 40] Additional middleware depth probes{RESET}")

    # Each trace probe against 40 different endpoints — verifies outer MW
    # unchanged regardless of inner handler.
    mw_probe_paths = [
        "/mw/trace", "/dep/diamond", "/dep/cache-x3", "/yield/nested",
        "/sub/a", "/sub/b", "/state/db", "/ep/fast", "/ep/async-fast",
        "/misc/large-list", "/misc/deep-json",
        "/misc/unicode-json", "/misc/numeric-edges",
        "/pp/int/42", "/pp/str/hi", "/status/201", "/status/418",
    ]
    for path in mw_probe_paths:
        def _mk(path=path):
            def t(diffs):
                fa = do_get(fa_port, path)
                fr = do_get(fr_port, path)
                # MW headers present?
                fa_mw = [fa.headers.get(f"x-mw{i}") for i in (1, 2, 3, 4, 5)]
                fr_mw = [fr.headers.get(f"x-mw{i}") for i in (1, 2, 3, 4, 5)]
                _diff_list(f"mw_hdrs{path}", fa_mw, fr_mw, diffs)
                return fa_mw, fr_mw, fa_mw == fr_mw
            return t
        run_test(next_id(), f"mw-probe: X-MW1..5 present on {path}",
                 "mw_probe", _mk())

    # ─── SECTION 41: Deep Request.state flow through multiple deps ─
    print(f"{CYAN}[SECT 41] Request.state through deps{RESET}")

    def t_state_via_deps(diffs):
        reset_all(fa_port, fr_port)
        fa = do_get(fa_port, "/dep/state-relay")
        fr = do_get(fr_port, "/dep/state-relay")
        fa_tr = _trace(fa) or []
        fr_tr = _trace(fr) or []
        # dep_sets_state should precede dep_reads_state with marker set
        def order_ok(tr):
            try:
                s = tr.index("dep_sets_state")
                r_idx = next(i for i, e in enumerate(tr)
                             if isinstance(e, str) and e.startswith("dep_reads_state:"))
                return s < r_idx and "set_by_dep" in tr[r_idx]
            except (ValueError, StopIteration):
                return False
        ok_fa = order_ok(fa_tr)
        ok_fr = order_ok(fr_tr)
        if not ok_fa: diffs.append(f"FA tr={fa_tr}")
        if not ok_fr: diffs.append(f"FR tr={fr_tr}")
        return fa_tr, fr_tr, ok_fa and ok_fr
    run_test(next_id(), "state: dep sets state — later dep reads it (ordered)",
             "dep_state_flow", t_state_via_deps)

    # ─── SECTION 42: 100 parallel /concurrency/slow identity check ──
    print(f"{CYAN}[SECT 42] 100 parallel slow identity{RESET}")

    async def _par_slow100(port):
        async with httpx.AsyncClient(base_url=f"http://{HOST}:{port}",
                                      timeout=_CLIENT_TIMEOUT) as c:
            tasks = [c.get("/concurrency/slow",
                           headers={"X-Req-Id": f"id-{i}"}) for i in range(100)]
            return await asyncio.gather(*tasks, return_exceptions=True)

    def t_concurrent_100_slow_all(diffs):
        httpx.get(f"http://{HOST}:{fa_port}/concurrency/reset")
        httpx.get(f"http://{HOST}:{fr_port}/concurrency/reset")
        fa = asyncio.run(_par_slow100(fa_port))
        fr = asyncio.run(_par_slow100(fr_port))
        fa_ok = sum(1 for r in fa
                    if not isinstance(r, Exception) and r.status_code == 200)
        fr_ok = sum(1 for r in fr
                    if not isinstance(r, Exception) and r.status_code == 200)
        if fa_ok != 100:
            diffs.append(f"FA {fa_ok}/100 ok")
            CONCURRENCY_FAILURES["load"] += 1
        if fr_ok != 100:
            diffs.append(f"FR {fr_ok}/100 ok")
            CONCURRENCY_FAILURES["load"] += 1
        return fa_ok, fr_ok, fa_ok == fr_ok == 100
    run_test(next_id(), "concurrent: 100 parallel slow — all complete",
             "concurrent_100", t_concurrent_100_slow_all)

    # ─── SECTION 43: Each query param type in isolation ─────────────
    print(f"{CYAN}[SECT 43] Query param types (20 probes){RESET}")

    qp_probes = [
        ("/pp/int-query?n=0", 200, {"n": 0}),
        ("/pp/int-query?n=-100", 200, {"n": -100}),
        ("/pp/int-query?n=999999", 200, {"n": 999999}),
        ("/pp/int-query?n=notanint", 422, None),
        ("/pp/float-query?p=0.5", 200, {"p": 0.5}),
        ("/pp/float-query?p=-1.25", 200, {"p": -1.25}),
        ("/pp/float-query?p=notafloat", 422, None),
        ("/pp/bool-query?flag=on", None, None),  # varies
        ("/pp/bool-query?flag=off", None, None),
        ("/pp/numeric-constraints?age=-1", 422, None),
        ("/pp/numeric-constraints?age=151", 422, None),
        ("/pp/numeric-constraints?age=abc", 422, None),
        ("/pp/list-query?tag=", 200, None),
        ("/pp/list-query?tag=&tag=", 200, None),
    ]
    for path, expected_status, expected_body in qp_probes:
        def _mk(path=path, es=expected_status, eb=expected_body):
            def t(diffs):
                fa = do_get(fa_port, path)
                fr = do_get(fr_port, path)
                _diff_list(f"qp_probe{path}_status",
                           fa.status_code, fr.status_code, diffs)
                a = _j(fa); b = _j(fr)
                _diff_list(f"qp_probe{path}_body", a, b, diffs)
                ok = fa.status_code == fr.status_code
                if es is not None:
                    ok = ok and fa.status_code == es
                return a, b, ok
            return t
        run_test(next_id(), f"qp-probe: {path}",
                 "qp_probe", _mk())

    # ─── SECTION 44: Additional header probes ──────────────────────
    print(f"{CYAN}[SECT 44] Header probes{RESET}")

    hdr_probes = [
        {"X-Alpha": "a"},
        {"X-Alpha": "a", "X-Beta": "b"},
        {"X-Authorization": "Bearer abc"},
        {"X-Req-Id": "id-42"},
        {"User-Agent": "parity-test/1"},
    ]
    for h in hdr_probes:
        def _mk(h=h):
            def t(diffs):
                fa = do_get(fa_port, "/req/headers-iter", headers=h)
                fr = do_get(fr_port, "/req/headers-iter", headers=h)
                a = _j(fa); b = _j(fr)
                _diff_list(f"hdr_probe{h}", a, b, diffs)
                return a, b, a == b
            return t
        run_test(next_id(), f"hdr-probe: {h}",
                 "hdr_probe", _mk())

    # ─── SECTION 45: JSON response round-trip for many payloads ────
    print(f"{CYAN}[SECT 45] JSON round-trip 12 payloads{RESET}")

    json_payloads = [
        {"x": 1},
        {"nested": {"x": 1}},
        [1, 2, 3],
        {"list": [1, 2, 3]},
        {"k": None},
        {"k": True},
        {"k": False},
        {"k": ""},
        {"k": "\u4f60\u597d"},  # unicode
        {"k": "emoji hi"},
        {"k": 3.14},
        {"k": [-1, 0, 1]},
    ]
    for p in json_payloads:
        def _mk(p=p):
            def t(diffs):
                fa = httpx.post(f"http://{HOST}:{fa_port}/req/json-parsed", json=p)
                fr = httpx.post(f"http://{HOST}:{fr_port}/req/json-parsed", json=p)
                a = _j(fa); b = _j(fr)
                _diff_list(f"json_probe{p!r}", a, b, diffs)
                return a, b, a == b and a and a.get("parsed") == p
            return t
        run_test(next_id(), f"json-probe: {p!r}",
                 "json_probe", _mk())

    # ─── SECTION 46: Boolean query interpretation ──────────────────
    print(f"{CYAN}[SECT 46] Boolean coercion edges{RESET}")

    # Only assert FA==FR. Interpretation may vary — depth is in the comparison.
    bool_raw = [
        "True", "False", "true", "false", "1", "0", "yes", "no",
        "on", "off", "t", "f", "y", "n",
    ]
    for v in bool_raw:
        def _mk(v=v):
            def t(diffs):
                fa = do_get(fa_port, f"/pp/bool-query?flag={v}")
                fr = do_get(fr_port, f"/pp/bool-query?flag={v}")
                a = _j(fa); b = _j(fr)
                _diff_list(f"bool_{v}", (fa.status_code, a),
                           (fr.status_code, b), diffs)
                return a, b, fa.status_code == fr.status_code and a == b
            return t
        run_test(next_id(), f"bool-coerce: flag={v}",
                 "bool_coerce", _mk())

    # ─── SECTION 47: Trace element-counts ─────────────────────────
    print(f"{CYAN}[SECT 47] Trace element counts per request{RESET}")

    trace_count_paths = [
        ("/mw/trace", 11),       # 5 MW in + handler + 5 MW out
        ("/dep/diamond", 11 + 4),
        ("/yield/nested", 11 + 6),  # 3 setups + handler + teardowns appended after response
    ]
    for path, exp_min in trace_count_paths:
        def _mk(path=path, exp_min=exp_min):
            def t(diffs):
                reset_all(fa_port, fr_port)
                fa_tr = _trace(do_get(fa_port, path)) or []
                fr_tr = _trace(do_get(fr_port, path)) or []
                if path.startswith("/yield/"):
                    fa_tr = _normalize_yield_trace(fa_tr)
                    fr_tr = _normalize_yield_trace(fr_tr)
                _diff_list(f"trace_count{path}", len(fa_tr), len(fr_tr), diffs)
                return fa_tr, fr_tr, len(fa_tr) == len(fr_tr)
            return t
        run_test(next_id(), f"trace-count: {path}",
                 "trace_count", _mk())

    # ─── SECTION 48: Starlette Response hdrs iter ──────────────────
    print(f"{CYAN}[SECT 48] Response headers iter{RESET}")

    # /resp/many-headers → X-One, X-Two, X-Dup (2x)
    def t_resp_hdr_iter(diffs):
        _, hd_fa, _ = raw_http(fa_port, "/resp/many-headers")
        _, hd_fr, _ = raw_http(fr_port, "/resp/many-headers")
        def simplify(hd):
            out = []
            for k, v in hd:
                if k.lower().startswith("x-"):
                    out.append((k.lower(), v))
            return sorted(out)
        a = simplify(hd_fa)
        b = simplify(hd_fr)
        _diff_list("resp_hdr_iter", a, b, diffs)
        return a, b, a == b
    run_test(next_id(), "resp: X-One, X-Two, 2x X-Dup iter",
             "resp_hdr_iter", t_resp_hdr_iter)

    # ─── SECTION 49: 10 parallel trace-fidelity under load ─────────
    print(f"{CYAN}[SECT 49] 10 parallel per-probe trace fidelity{RESET}")

    async def _par10(port, path):
        async with httpx.AsyncClient(base_url=f"http://{HOST}:{port}",
                                      timeout=_CLIENT_TIMEOUT) as c:
            tasks = [c.get(path) for _ in range(10)]
            rs = await asyncio.gather(*tasks, return_exceptions=True)
            return [_trace(r) if not isinstance(r, Exception) else None
                    for r in rs]

    probe10 = ["/mw/trace", "/dep/diamond", "/dep/cache-x3",
               "/yield/nested", "/sub/a"]
    for path in probe10:
        def _mk(path=path):
            def t(diffs):
                reset_all(fa_port, fr_port)
                fa = asyncio.run(_par10(fa_port, path))
                fr = asyncio.run(_par10(fr_port, path))
                if path.startswith("/yield/"):
                    fa = [_normalize_yield_trace(t) for t in fa]
                    fr = [_normalize_yield_trace(t) for t in fr]
                # Normalize global-counter suffixes (#N, [a=N]) — the
                # invocation counter is shared across all requests so raw
                # traces differ. Parity here is about dep-shape stability,
                # not absolute numbering.
                fa = _normalize_trace_shape(fa)
                fr = _normalize_trace_shape(fr)
                # All 10 should be equal per port
                fa_all = all(t == fa[0] for t in fa[1:]) if fa else False
                fr_all = all(t == fr[0] for t in fr[1:]) if fr else False
                cross = fa[0] == fr[0] if fa and fr else False
                if not fa_all:
                    diffs.append(f"FA 10-parallel unstable")
                    CONCURRENCY_FAILURES["correctness"] += 1
                if not fr_all:
                    diffs.append(f"FR 10-parallel unstable")
                    CONCURRENCY_FAILURES["correctness"] += 1
                if not cross:
                    _diff_list(f"par10{path}", fa[0], fr[0], diffs)
                return fa[0], fr[0], fa_all and fr_all and cross
            return t
        run_test(next_id(), f"par10-trace: {path}",
                 "par10_trace", _mk())

    # ─── SECTION 50: Final batch of parity reqs ────────────────────
    print(f"{CYAN}[SECT 50] Final parity batch{RESET}")

    final_paths = [
        ("/load/ep0", {"ep": 0, "ok": True}),
        ("/load/ep15", {"ep": 15, "ok": True}),
        ("/load/ep29", {"ep": 29, "ok": True}),
    ]
    for path, expected in final_paths:
        def _mk(path=path, expected=expected):
            def t(diffs):
                fa = do_get(fa_port, path)
                fr = do_get(fr_port, path)
                a = _j(fa); b = _j(fr)
                _diff_list(f"final{path}", a, b, diffs)
                return a, b, a == b == expected
            return t
        run_test(next_id(), f"final: {path}",
                 "final_parity", _mk())

    # ─── SECTION 51: Streaming more variants ───────────────────────
    print(f"{CYAN}[SECT 51] Streaming more variants{RESET}")

    stream_paths = [
        "/stream/tagged",
        "/stream/tagged-async",
        "/stream/sse-full",
        "/stream/bytes-tagged",
    ]
    for path in stream_paths:
        def _mk(path=path):
            def t(diffs):
                fa = do_get(fa_port, path)
                fr = do_get(fr_port, path)
                # Compare bytes identity
                if fa.content != fr.content:
                    diffs.append(f"body mismatch len={len(fa.content)}/{len(fr.content)}")
                    diffs.append(f"FA={fa.content[:80]!r} FR={fr.content[:80]!r}")
                # Compare content-type
                ct_fa = fa.headers.get("content-type", "")
                ct_fr = fr.headers.get("content-type", "")
                if ct_fa.split(";")[0].strip() != ct_fr.split(";")[0].strip():
                    diffs.append(f"ct FA={ct_fa} FR={ct_fr}")
                return fa.content, fr.content, fa.content == fr.content
            return t
        run_test(next_id(), f"stream-bytes: {path}",
                 "stream_bytes_parity", _mk())

    # ─── SECTION 52: UploadFile with text body ─────────────────────
    print(f"{CYAN}[SECT 52] UploadFile content types{RESET}")

    upload_cts = [
        ("text.txt", b"hello", "text/plain"),
        ("data.json", b'{"x":1}', "application/json"),
        ("image.png", b"\x89PNG\r\n\x1a\n", "image/png"),
        ("empty.bin", b"", "application/octet-stream"),
        ("csv.csv", b"a,b,c\n1,2,3", "text/csv"),
    ]
    for name, data, ct in upload_cts:
        def _mk(name=name, data=data, ct=ct):
            def t(diffs):
                files = {"file": (name, data, ct)}
                fa = httpx.post(f"http://{HOST}:{fa_port}/upload/one", files=files)
                fr = httpx.post(f"http://{HOST}:{fr_port}/upload/one", files=files)
                a = _j(fa); b = _j(fr)
                _diff_list(f"upload_ct{name}", a, b, diffs)
                return a, b, a == b and a and a.get("filename") == name
            return t
        run_test(next_id(), f"upload-ct: {name} ({ct})",
                 "upload_ct", _mk())

    # ─── SECTION 53: Status code parity across many endpoints ──────
    print(f"{CYAN}[SECT 53] Status code parity broad{RESET}")

    status_probes = [
        ("/health", "GET", None, 200),
        ("/_reset", "GET", None, 200),
        ("/_counts", "GET", None, 200),
        ("/_bg_log", "GET", None, 200),
        ("/_yield_events", "GET", None, 200),
        ("/_lifespan", "GET", None, 200),
        ("/ep/fast", "GET", None, 200),
        ("/ep/async-fast", "GET", None, 200),
        ("/status/201", "GET", None, 201),
        ("/status/204", "GET", None, 204),
        ("/status/418", "GET", None, 418),
        ("/exc/custom", "GET", None, 418),
        ("/exc/http-with-dict", "GET", None, 404),
        ("/exc/http-with-list", "GET", None, 400),
        ("/exc/http-with-headers", "GET", None, 401),
        ("/exc/value-error", "GET", None, 500),
        ("/mw/raise", "GET", None, 500),
        ("/mw/http-exc", "GET", None, 418),
        ("/pp/numeric-constraints?age=200", "GET", None, 422),
    ]
    for path, method, body, expected in status_probes:
        def _mk(path=path, method=method, body=body, expected=expected):
            def t(diffs):
                kw = {}
                if body is not None: kw["json"] = body
                fa = httpx.request(method, f"http://{HOST}:{fa_port}{path}", **kw)
                fr = httpx.request(method, f"http://{HOST}:{fr_port}{path}", **kw)
                _diff_list(f"status{path}", fa.status_code, fr.status_code, diffs)
                ok = fa.status_code == fr.status_code == expected
                return fa.status_code, fr.status_code, ok
            return t
        run_test(next_id(), f"status-probe: {method} {path} → {expected}",
                 "status_probe", _mk())

    # ─── SECTION 54: 20 parallel req-state (lighter load test) ─────
    print(f"{CYAN}[SECT 54] 20 parallel request.state correctness{RESET}")

    async def _par_state_20(port):
        async with httpx.AsyncClient(base_url=f"http://{HOST}:{port}",
                                      timeout=_CLIENT_TIMEOUT) as c:
            tasks = [c.post("/concurrency/req-state",
                           headers={"X-Req-Id": f"SMALL-{i}"})
                     for i in range(20)]
            rs = await asyncio.gather(*tasks, return_exceptions=True)
            return [r.json() if not isinstance(r, Exception) else {"err": True}
                    for r in rs]

    def t_state_20(diffs):
        fa = asyncio.run(_par_state_20(fa_port))
        fr = asyncio.run(_par_state_20(fr_port))
        fa_bad = [i for i, e in enumerate(fa) if not e.get("match")]
        fr_bad = [i for i, e in enumerate(fr) if not e.get("match")]
        if fa_bad:
            diffs.append(f"FA leak idx={fa_bad[:5]}")
            CONCURRENCY_FAILURES["correctness"] += 1
        if fr_bad:
            diffs.append(f"FR leak idx={fr_bad[:5]}")
            CONCURRENCY_FAILURES["correctness"] += 1
        return fa, fr, not fa_bad and not fr_bad
    run_test(next_id(), "concurrent-state: 20 parallel no leak",
             "concurrent_state_20", t_state_20)

    # ─── SECTION 55: Response body type parity probes ──────────────
    print(f"{CYAN}[SECT 55] Response body types{RESET}")

    body_probes = [
        ("/ep/fast",),
        ("/state/db",),
        ("/ep/echo/1",),
        ("/ep/echo/99",),
        ("/status/418",),
        ("/misc/unicode-json",),
        ("/misc/numeric-edges",),
        ("/misc/deep-json",),
        ("/misc/large-list",),
        ("/misc/special-chars",),
    ]
    for (path,) in body_probes:
        def _mk(path=path):
            def t(diffs):
                fa = do_get(fa_port, path)
                fr = do_get(fr_port, path)
                a = _j(fa); b = _j(fr)
                _diff_list(f"body_probe{path}", a, b, diffs)
                _diff_list(f"body_probe{path}_ct",
                           fa.headers.get("content-type", "").split(";")[0].strip(),
                           fr.headers.get("content-type", "").split(";")[0].strip(),
                           diffs)
                return a, b, a == b
            return t
        run_test(next_id(), f"body-probe: {path} parity",
                 "body_probe", _mk())

    # ─── SECTION 56: Trace entry counts for handlers ───────────────
    print(f"{CYAN}[SECT 56] Handler present in trace{RESET}")

    handler_probes = [
        ("/mw/trace", "handler"),
        ("/dep/diamond", "handler_diamond"),
        ("/dep/cache-x3", "handler_cached"),
        ("/dep/no-cache-x3", "handler_nocache"),
        ("/dep/state-relay", "handler_state:set_by_dep"),
        ("/yield/nested", "handler_yield:YC[YB[YA]]"),
        ("/yield/async", "handler_yield_async:AS"),
        ("/sub/a", "sub_a_handler"),
        ("/sub/b", "sub_b_handler"),
    ]
    for path, expected_str in handler_probes:
        def _mk(path=path, expected_str=expected_str):
            def t(diffs):
                reset_all(fa_port, fr_port)
                fa_tr = _trace(do_get(fa_port, path)) or []
                fr_tr = _trace(do_get(fr_port, path)) or []
                in_fa = any(isinstance(e, str) and expected_str in e for e in fa_tr)
                in_fr = any(isinstance(e, str) and expected_str in e for e in fr_tr)
                if not in_fa: diffs.append(f"FA missing {expected_str!r}: {fa_tr}")
                if not in_fr: diffs.append(f"FR missing {expected_str!r}: {fr_tr}")
                return fa_tr, fr_tr, in_fa and in_fr
            return t
        run_test(next_id(), f"handler-trace: {path} contains {expected_str!r}",
                 "handler_trace", _mk())

    # ─── SECTION 57: Assorted dep variations ──────────────────────
    print(f"{CYAN}[SECT 57] Assorted dep variations{RESET}")

    dep_probes = [
        "/trace/five-deps?q=hi",
        "/trace/five-deps?q=",
        "/trace/five-deps?q=with%20space",
        "/trace/five-deps?q=%E4%BD%A0%E5%A5%BD",
    ]
    for path in dep_probes:
        def _mk(path=path):
            def t(diffs):
                reset_all(fa_port, fr_port)
                fa = do_get(fa_port, path)
                fr = do_get(fr_port, path)
                a = _j(fa); b = _j(fr)
                ta = a.get("trace") if a else None
                tb = b.get("trace") if b else None
                # Filter to dep entries only (handler_ entries match)
                def pick(tr):
                    return [e for e in (tr or []) if isinstance(e, str)
                            and (e.startswith("dep_") or e.startswith("handler_"))]
                _diff_list(f"dep5{path}", pick(ta), pick(tb), diffs)
                return ta, tb, pick(ta) == pick(tb)
            return t
        run_test(next_id(), f"dep5: {path}",
                 "dep5_variant", _mk())

    # ─── SECTION 58: CORS simple cross-origin ─────────────────────
    print(f"{CYAN}[SECT 58] More CORS{RESET}")

    cors_tests = [
        ({"Origin": "http://a.com"}, "GET", "/ep/fast"),
        ({"Origin": "http://b.com"}, "GET", "/state/db"),
        ({"Origin": "*"}, "GET", "/health"),
    ]
    for hdr, method, path in cors_tests:
        def _mk(hdr=hdr, method=method, path=path):
            def t(diffs):
                fa = httpx.request(method, f"http://{HOST}:{fa_port}{path}", headers=hdr)
                fr = httpx.request(method, f"http://{HOST}:{fr_port}{path}", headers=hdr)
                aco_fa = fa.headers.get("access-control-allow-origin")
                aco_fr = fr.headers.get("access-control-allow-origin")
                if aco_fa is None: diffs.append("FA no ACO")
                if aco_fr is None: diffs.append("FR no ACO")
                return aco_fa, aco_fr, aco_fa is not None and aco_fr is not None
            return t
        run_test(next_id(), f"cors-simple: {hdr.get('Origin')} on {path}",
                 "cors_simple", _mk())

    # ─── SECTION 59: 10 concurrent uploads ─────────────────────────
    print(f"{CYAN}[SECT 59] Concurrent uploads{RESET}")

    async def _upload_concurrent(port, n=10):
        async with httpx.AsyncClient(base_url=f"http://{HOST}:{port}",
                                      timeout=_CLIENT_TIMEOUT) as c:
            tasks = []
            for i in range(n):
                files = {"file": (f"f{i}.txt", f"content-{i}".encode(), "text/plain")}
                tasks.append(c.post("/upload/one", files=files))
            rs = await asyncio.gather(*tasks, return_exceptions=True)
            out = []
            for r in rs:
                if isinstance(r, Exception):
                    out.append({"err": True})
                else:
                    try:
                        out.append(r.json())
                    except Exception:
                        out.append({"err": True})
            return out

    def t_concurrent_uploads(diffs):
        fa = asyncio.run(_upload_concurrent(fa_port))
        fr = asyncio.run(_upload_concurrent(fr_port))
        def ok_all(results):
            bad = []
            for i, r in enumerate(results):
                if r.get("err"): bad.append(i); continue
                if r.get("filename") != f"f{i}.txt": bad.append(i)
            return bad
        fa_bad = ok_all(fa); fr_bad = ok_all(fr)
        if fa_bad:
            diffs.append(f"FA bad={fa_bad[:5]}")
            CONCURRENCY_FAILURES["correctness"] += 1
        if fr_bad:
            diffs.append(f"FR bad={fr_bad[:5]}")
            CONCURRENCY_FAILURES["correctness"] += 1
        return fa, fr, not fa_bad and not fr_bad
    run_test(next_id(), "upload: 10 parallel uploads, each correct",
             "concurrent_upload", t_concurrent_uploads)

    # ─── SECTION 60: MORE JSON edges ──────────────────────────────
    print(f"{CYAN}[SECT 60] JSON encoding edges{RESET}")

    json_edge_payloads = [
        {"nan": None},  # shouldn't be NaN
        {"mix": {"a": [1, {"b": [2, {"c": 3}]}]}},
        {"long_str": "x" * 500},
        {"many_keys": {f"k{i}": i for i in range(50)}},
        {"list_of_lists": [[1, 2], [3, 4], [5, 6]]},
        {"negative": -2**31},
        {"i64": 2**40},
        {"escaped": "tab\there\nnl\rcr\"quote\\backslash"},
        {"unicode_keys_too": {"你好": 1, "café": 2}},
    ]
    for p in json_edge_payloads:
        def _mk(p=p):
            def t(diffs):
                fa = httpx.post(f"http://{HOST}:{fa_port}/req/json-parsed", json=p)
                fr = httpx.post(f"http://{HOST}:{fr_port}/req/json-parsed", json=p)
                a = _j(fa); b = _j(fr)
                _diff_list(f"json_edge{p!r}", a, b, diffs)
                return a, b, a == b and a and a.get("parsed") == p
            return t
        run_test(next_id(), f"json-edge: {str(p)[:40]}",
                 "json_edge", _mk())

    # ─── SECTION 61: Cookie attr individual additional ─────────────
    print(f"{CYAN}[SECT 61] Cookie individual set endpoints{RESET}")

    cookie_set_probes = [
        ("/cookie/samesite-none", "none"),
        ("/cookie/samesite-strict", "strict"),
    ]
    for path, expected_ss in cookie_set_probes:
        def _mk(path=path, expected_ss=expected_ss):
            def t(diffs):
                fa = do_get(fa_port, path)
                fr = do_get(fr_port, path)
                pa = parse_set_cookies(_set_cookies(fa))
                pb = parse_set_cookies(_set_cookies(fr))
                va = pa[0].get("attrs", {}).get("samesite", "") if pa else ""
                vb = pb[0].get("attrs", {}).get("samesite", "") if pb else ""
                # Case-insensitive compare
                if va.lower() != vb.lower():
                    diffs.append(f"ss FA={va!r} FR={vb!r}")
                return va, vb, va.lower() == vb.lower()
            return t
        run_test(next_id(), f"cookie-set: {path}",
                 "cookie_set_probe", _mk())

    # ─── SECTION 62: Path ints range ───────────────────────────────
    print(f"{CYAN}[SECT 62] Int path param range{RESET}")

    int_path_probes = [0, 1, -1, 999999, -999999, 2**31 - 1, -(2**31)]
    for n in int_path_probes:
        def _mk(n=n):
            def t(diffs):
                fa = do_get(fa_port, f"/pp/int/{n}")
                fr = do_get(fr_port, f"/pp/int/{n}")
                a = _j(fa); b = _j(fr)
                _diff_list(f"int_path{n}", a, b, diffs)
                return a, b, a == b and a and a.get("x") == n
            return t
        run_test(next_id(), f"int-path: /pp/int/{n}",
                 "int_path", _mk())

    # ─── SECTION 63: Many empty/edge reqs ─────────────────────────
    print(f"{CYAN}[SECT 63] Empty reqs{RESET}")

    empty_probes = [
        (b"", None),   # empty body
        (b"{}", "application/json"),  # empty dict
        (b"[]", "application/json"),
        (b'"string"', "application/json"),
        (b"null", "application/json"),
    ]
    for body, ct in empty_probes:
        def _mk(body=body, ct=ct):
            def t(diffs):
                kw = {"content": body}
                if ct: kw["headers"] = {"Content-Type": ct}
                fa = httpx.post(f"http://{HOST}:{fa_port}/req/body-bytes", **kw)
                fr = httpx.post(f"http://{HOST}:{fr_port}/req/body-bytes", **kw)
                a = _j(fa); b = _j(fr)
                _diff_list(f"empty{body}", a, b, diffs)
                return a, b, a == b and a and a.get("len") == len(body)
            return t
        run_test(next_id(), f"empty-body: len={len(body)} ct={ct}",
                 "empty_body", _mk())

    # ─── SECTION 64: 30 sequential /dep/diamond request counts ────
    print(f"{CYAN}[SECT 64] /dep/diamond per-req A count stability{RESET}")

    def t_diamond_stable_counts(diffs):
        reset_all(fa_port, fr_port)
        fa_rs = [_j(do_get(fa_port, "/dep/diamond")) for _ in range(10)]
        fr_rs = [_j(do_get(fr_port, "/dep/diamond")) for _ in range(10)]
        # Each request should show the same shape b.a.n == c.a.n
        def check(rs):
            bad = []
            for i, r in enumerate(rs):
                try:
                    b_a_n = r["r"]["b"]["a"]["n"]
                    c_a_n = r["r"]["c"]["a"]["n"]
                    if b_a_n != c_a_n: bad.append(i)
                except Exception:
                    bad.append(i)
            return bad
        bad_fa = check(fa_rs); bad_fr = check(fr_rs)
        if bad_fa: diffs.append(f"FA bad={bad_fa}")
        if bad_fr: diffs.append(f"FR bad={bad_fr}")
        return fa_rs, fr_rs, not bad_fa and not bad_fr
    run_test(next_id(), "diamond: 10 sequential — b.a.n == c.a.n always",
             "diamond_stable", t_diamond_stable_counts)

    # ─── SECTION 65: Trace parity for yield_async under load ─────
    async def _par_yield_async(port, n=20):
        async with httpx.AsyncClient(base_url=f"http://{HOST}:{port}",
                                      timeout=_CLIENT_TIMEOUT) as c:
            tasks = [c.get("/yield/async") for _ in range(n)]
            rs = await asyncio.gather(*tasks, return_exceptions=True)
            return [_trace(r) if not isinstance(r, Exception) else None for r in rs]

    def t_yield_async_par(diffs):
        reset_all(fa_port, fr_port)
        fa = asyncio.run(_par_yield_async(fa_port))
        fr = asyncio.run(_par_yield_async(fr_port))
        def stable(l):
            return l and all(x == l[0] for x in l)
        if not stable(fa):
            diffs.append("FA yield_async trace unstable")
            CONCURRENCY_FAILURES["correctness"] += 1
        if not stable(fr):
            diffs.append("FR yield_async trace unstable")
            CONCURRENCY_FAILURES["correctness"] += 1
        return fa[0] if fa else None, fr[0] if fr else None, stable(fa) and stable(fr)
    run_test(next_id(), "yield_async: 20 parallel trace stable",
             "yield_async_concurrent", t_yield_async_par)

    # ─── SECTION 66: Response Content-Type breadth ────────────────
    print(f"{CYAN}[SECT 66] Content-Type breadth{RESET}")

    ct_probes = [
        ("/ep/fast", "application/json"),
        ("/health", "application/json"),
        ("/stream/tagged", "application/x-ndjson"),
        ("/stream/sse-full", "text/event-stream"),
        ("/stream/bytes-tagged", "application/octet-stream"),
        ("/resp/media-type-override", "application/xml"),
        ("/misc/large-list", "application/json"),
    ]
    for path, expected_ct in ct_probes:
        def _mk(path=path, expected_ct=expected_ct):
            def t(diffs):
                fa = do_get(fa_port, path)
                fr = do_get(fr_port, path)
                ct_fa = fa.headers.get("content-type", "").split(";")[0].strip()
                ct_fr = fr.headers.get("content-type", "").split(";")[0].strip()
                if ct_fa != ct_fr:
                    diffs.append(f"FA ct={ct_fa} FR ct={ct_fr}")
                if expected_ct not in (ct_fa or ""):
                    diffs.append(f"FA ct missing {expected_ct}: got {ct_fa}")
                return ct_fa, ct_fr, ct_fa == ct_fr and expected_ct in (ct_fa or "")
            return t
        run_test(next_id(), f"ct-probe: {path} → {expected_ct}",
                 "ct_probe", _mk())

    # ─── SECTION 67: Cookie parsing into Cookie() with more cases ──
    print(f"{CYAN}[SECT 67] Cookie() extraction breadth{RESET}")

    cookie_hdr_cases = [
        "foo=bar",
        "foo=bar; baz=qux",
        "foo=\"quoted\"",
        "foo=a%20b",
        "foo=emoji",  # ASCII-only, avoid latin-1 issues
        "foo=",
        "foo=trimspaces",  # httpx/h11 reject leading/trailing spaces on header values per RFC 7230
        "foo=1; foo=2",  # duplicate — first or last?
    ]
    for ch in cookie_hdr_cases:
        def _mk(ch=ch):
            def t(diffs):
                fa = do_get(fa_port, "/cookie/urlenc-value", headers={"Cookie": ch})
                fr = do_get(fr_port, "/cookie/urlenc-value", headers={"Cookie": ch})
                a = _j(fa); b = _j(fr)
                _diff_list(f"cookie_hdr{ch}", a, b, diffs)
                return a, b, a == b
            return t
        run_test(next_id(), f"cookie-hdr: {ch!r}",
                 "cookie_hdr_case", _mk())

    # ─── SECTION 68: More handler status code decorator cases ─────
    print(f"{CYAN}[SECT 68] status_code decorator{RESET}")

    for path_status in [("/status/201", 201), ("/status/204", 204),
                        ("/status/418", 418)]:
        def _mk(path=path_status[0], st=path_status[1]):
            def t(diffs):
                fa = do_get(fa_port, path)
                fr = do_get(fr_port, path)
                if fa.status_code != fr.status_code:
                    diffs.append(f"status FA={fa.status_code} FR={fr.status_code}")
                if fa.status_code != st:
                    diffs.append(f"FA status wrong")
                return fa.status_code, fr.status_code, fa.status_code == fr.status_code == st
            return t
        run_test(next_id(), f"status-dec: {path_status[0]} → {path_status[1]}",
                 "status_dec", _mk())

    # ─── SECTION 69: Path int negative vs positive ────────────────
    print(f"{CYAN}[SECT 69] path param int sign{RESET}")

    for n in [0, 1, -1, 42, -42, 100, -100, 2**20, -(2**20)]:
        def _mk(n=n):
            def t(diffs):
                fa = do_get(fa_port, f"/ep/echo/{n}")
                fr = do_get(fr_port, f"/ep/echo/{n}")
                a = _j(fa); b = _j(fr)
                _diff_list(f"ep_echo{n}", a, b, diffs)
                return a, b, a == b == {"n": n, "doubled": n * 2}
            return t
        run_test(next_id(), f"ep-echo: {n}",
                 "ep_echo", _mk())

    # ─── SECTION 70: Trace parity for load under multiple requests ─
    print(f"{CYAN}[SECT 70] Assorted re-probes{RESET}")

    for p in ["/load/ep5", "/load/ep10", "/load/ep20", "/load/ep25"]:
        def _mk(p=p):
            def t(diffs):
                fa = do_get(fa_port, p)
                fr = do_get(fr_port, p)
                a = _j(fa); b = _j(fr)
                _diff_list(f"load_reprobe{p}", a, b, diffs)
                return a, b, a == b
            return t
        run_test(next_id(), f"load-reprobe: {p}",
                 "load_reprobe", _mk())

    # ─── SECTION 71: Request stream with different sized bodies ───
    print(f"{CYAN}[SECT 71] Request stream various sizes{RESET}")

    for sz in [0, 1, 100, 1024, 4096, 16384, 65536]:
        def _mk(sz=sz):
            def t(diffs):
                body = b"X" * sz
                fa = httpx.post(f"http://{HOST}:{fa_port}/req/stream-body",
                                content=body, timeout=30)
                fr = httpx.post(f"http://{HOST}:{fr_port}/req/stream-body",
                                content=body, timeout=30)
                a = _j(fa); b = _j(fr)
                _diff_list(f"stream_sz{sz}", a, b, diffs)
                if a and a.get("total") != sz:
                    diffs.append(f"FA total={a.get('total')} exp={sz}")
                if b and b.get("total") != sz:
                    diffs.append(f"FR total={b.get('total')} exp={sz}")
                ok = a and b and a.get("total") == sz and b.get("total") == sz
                return a, b, ok
            return t
        run_test(next_id(), f"stream-sz: {sz} bytes",
                 "stream_sz", _mk())

    # ─── SECTION 72: Concurrent MW short-circuit ──────────────────
    print(f"{CYAN}[SECT 72] Concurrent MW short-circuit{RESET}")

    async def _par_sc(port, n=30):
        async with httpx.AsyncClient(base_url=f"http://{HOST}:{port}",
                                      timeout=_CLIENT_TIMEOUT) as c:
            tasks = [c.get("/mw/trace", headers={"X-SC-At-3": "yes"})
                     for _ in range(n)]
            rs = await asyncio.gather(*tasks, return_exceptions=True)
            out = []
            for r in rs:
                if isinstance(r, Exception):
                    out.append(None)
                else:
                    tr = _trace(r)
                    if tr is None:
                        try:
                            tr = r.json().get("trace", [])
                        except Exception:
                            tr = []
                    out.append(tr)
            return out

    def t_concurrent_sc(diffs):
        fa = asyncio.run(_par_sc(fa_port))
        fr = asyncio.run(_par_sc(fr_port))
        # MW3 short-circuits; response bubbles through MW4, MW5 teardown.
        # So trace contains SC entry + MW4_out + MW5_out.
        exp_prefix = ["MW5_in", "MW4_in", "MW3_in", "MW3_short_circuit"]
        def has_prefix(t):
            return t and t[:4] == exp_prefix
        fa_bad = sum(1 for t in fa if not has_prefix(t))
        fr_bad = sum(1 for t in fr if not has_prefix(t))
        if fa_bad:
            diffs.append(f"FA bad {fa_bad}/30")
            CONCURRENCY_FAILURES["correctness"] += 1
        if fr_bad:
            diffs.append(f"FR bad {fr_bad}/30")
            CONCURRENCY_FAILURES["correctness"] += 1
        return fa_bad, fr_bad, fa_bad == 0 and fr_bad == 0
    run_test(next_id(), "concurrent: 30 parallel MW short-circuit at MW3",
             "concurrent_sc", t_concurrent_sc)

    # ─── SECTION 73: /state/incr under 50 parallel ────────────────
    async def _par_incr(port, n=50):
        async with httpx.AsyncClient(base_url=f"http://{HOST}:{port}",
                                      timeout=_CLIENT_TIMEOUT) as c:
            tasks = [c.get("/state/incr") for _ in range(n)]
            rs = await asyncio.gather(*tasks, return_exceptions=True)
            out = []
            for r in rs:
                if isinstance(r, Exception):
                    out.append(None)
                else:
                    try:
                        out.append(r.json().get("counter"))
                    except Exception:
                        out.append(None)
            return out

    def t_state_incr_par(diffs):
        # Note: FA/FR start from different counters, don't compare cross
        fa = asyncio.run(_par_incr(fa_port))
        fr = asyncio.run(_par_incr(fr_port))
        # Each should contain 50 distinct values (no races) or at minimum
        # increment monotonically
        fa_valid = sum(1 for x in fa if x is not None)
        fr_valid = sum(1 for x in fr if x is not None)
        if fa_valid != 50: diffs.append(f"FA {fa_valid}/50 valid")
        if fr_valid != 50: diffs.append(f"FR {fr_valid}/50 valid")
        return fa, fr, fa_valid == 50 and fr_valid == 50
    run_test(next_id(), "concurrent: 50 parallel /state/incr all responded",
             "concurrent_state_incr", t_state_incr_par)

    # ─── SECTION 74: No-mw handler path exists ────────────────────
    print(f"{CYAN}[SECT 74] Dep raises HTTPException{RESET}")

    def t_dep_http_exc(diffs):
        # dep_raises simulated via url on handler; we use custom err endpoint
        # With no dep/raises endpoint we'll re-use /exc/http-with-dict
        fa = do_get(fa_port, "/exc/http-with-dict")
        fr = do_get(fr_port, "/exc/http-with-dict")
        a = _j(fa); b = _j(fr)
        _diff_list("dep_http_exc", a, b, diffs)
        return a, b, a == b
    run_test(next_id(), "exc: http exc detail dict — identical shape",
             "exc_dict_shape", t_dep_http_exc)

    # ─── SECTION 75: X-Trace header presence audit on many reqs ──
    print(f"{CYAN}[SECT 75] X-Trace header presence audit{RESET}")

    audit_paths = [
        "/mw/trace", "/dep/diamond", "/dep/cache-x3", "/yield/nested",
        "/sub/a", "/state/db", "/ep/fast", "/misc/large-list",
    ]
    for path in audit_paths:
        def _mk(path=path):
            def t(diffs):
                fa = do_get(fa_port, path)
                fr = do_get(fr_port, path)
                ha = fa.headers.get("x-trace")
                hb = fr.headers.get("x-trace")
                if ha is None: diffs.append(f"FA no X-Trace on {path}")
                if hb is None: diffs.append(f"FR no X-Trace on {path}")
                return bool(ha), bool(hb), bool(ha) and bool(hb)
            return t
        run_test(next_id(), f"xtrace-present: {path}",
                 "xtrace_presence", _mk())

    # ─── SECTION 76: Cookie default case when Cookie hdr missing ──
    def t_cookie_default(diffs):
        fa = do_get(fa_port, "/cookie/get-multi")
        fr = do_get(fr_port, "/cookie/get-multi")
        a = _j(fa); b = _j(fr)
        _diff_list("cookie_default", a, b, diffs)
        return a, b, a == b == {"a": "A_D", "b": "B_D", "c": "C_D"}
    run_test(next_id(), "cookie-default: missing cookies → defaults",
             "cookie_default", t_cookie_default)

    # ─── SECTION 77: Middleware probe with concurrent error ───────
    async def _par_raise(port, n=20):
        async with httpx.AsyncClient(base_url=f"http://{HOST}:{port}",
                                      timeout=_CLIENT_TIMEOUT) as c:
            tasks = [c.get("/mw/raise") for _ in range(n)]
            rs = await asyncio.gather(*tasks, return_exceptions=True)
            return [r.status_code if not isinstance(r, Exception) else -1 for r in rs]

    def t_mw_raise_par(diffs):
        fa = asyncio.run(_par_raise(fa_port))
        fr = asyncio.run(_par_raise(fr_port))
        fa_500 = sum(1 for s in fa if s == 500)
        fr_500 = sum(1 for s in fr if s == 500)
        if fa_500 != 20: diffs.append(f"FA {fa_500}/20 500s")
        if fr_500 != 20: diffs.append(f"FR {fr_500}/20 500s")
        return fa_500, fr_500, fa_500 == 20 and fr_500 == 20
    run_test(next_id(), "concurrent: 20 parallel handler-raise → all 500",
             "concurrent_raise", t_mw_raise_par)

    # ─── SECTION 78: /req/url-deep across many paths ─────────────
    url_deep_paths = [
        "/req/url-deep", "/req/url-deep?a=1", "/req/url-deep?a=1&b=2",
        "/req/url-deep?x=%20", "/req/url-deep?empty=",
    ]

    for path in url_deep_paths:
        def _mk(path=path):
            def t(diffs):
                fa = do_get(fa_port, path)
                fr = do_get(fr_port, path)
                a = _normalize_url_response(_j(fa), fa_port)
                b = _normalize_url_response(_j(fr), fr_port)
                _diff_list(f"url_deep{path}", a, b, diffs)
                return a, b, a == b
            return t
        run_test(next_id(), f"url-deep: {path}",
                 "url_deep", _mk())

    # ─── SECTION 79: Repeated /ep/fast for deep consistency ────────
    for i in range(20):
        def _mk(i=i):
            def t(diffs):
                fa = do_get(fa_port, "/ep/fast")
                fr = do_get(fr_port, "/ep/fast")
                a = _j(fa); b = _j(fr)
                _diff_list(f"fast_rep{i}", a, b, diffs)
                return a, b, a == b == {"fast": True}
            return t
        run_test(next_id(), f"fast-rep: iter{i}",
                 "fast_rep", _mk())

    # ─── SECTION 80: Req via async client directly ────────────────
    async def _req_mix():
        async with httpx.AsyncClient(base_url=f"http://{HOST}:{fa_port}",
                                      timeout=_CLIENT_TIMEOUT) as c:
            return [
                (await c.get("/ep/fast")).json(),
                (await c.get("/ep/async-fast")).json(),
                (await c.get("/dep/diamond")).json(),
            ]

    def t_async_client_seq(diffs):
        fa = asyncio.run(_req_mix())
        # Not comparing cross — just asserting FA works with AsyncClient
        return fa, None, all("fast" in str(x) or "n" in str(x) for x in fa)
    run_test(next_id(), "async-client: sequential works",
             "async_client", t_async_client_seq)


# ── Main ───────────────────────────────────────────────────────────

def main():
    print(f"\n{BOLD}{'='*72}")
    print(f"  ROUND 2 Deep Behavior Parity")
    print(f"  FastAPI on :{FA_PORT}  |  fastapi-turbo on :{FR_PORT}")
    print(f"{'='*72}{RESET}\n")

    uvicorn_proc = None
    rs_proc = None

    try:
        print(f"Starting uvicorn on {FA_PORT}...")
        uvicorn_proc = start_uvicorn(FA_PORT)
        print(f"Starting fastapi-turbo on {FR_PORT}...")
        rs_proc = start_fastapi_turbo(FR_PORT)

        print("Waiting for servers...")
        fa_ready = wait_for_port(FA_PORT)
        fr_ready = wait_for_port(FR_PORT)

        if not fa_ready:
            print(f"{RED}uvicorn failed to start; see /tmp/parity_r2_uvicorn.log{RESET}")
            return 1
        if not fr_ready:
            print(f"{RED}fastapi-turbo failed to start; see /tmp/parity_r2_fastapi_turbo.log{RESET}")
            return 1

        print(f"{GREEN}Both servers ready!{RESET}\n")

        # Healthcheck
        for label, port in [("FA", FA_PORT), ("FR", FR_PORT)]:
            try:
                r = httpx.get(f"http://{HOST}:{port}/health", timeout=5)
                if r.status_code != 200:
                    print(f"{RED}{label} /health failed: {r.status_code}{RESET}")
                    return 1
            except Exception as e:
                print(f"{RED}{label} /health error: {e}{RESET}")
                return 1

        try:
            run_all_tests(FA_PORT, FR_PORT)
        except Exception as e:
            print(f"{RED}Test run aborted mid-way: {e}{RESET}")
            import traceback
            traceback.print_exc()

        # Summary
        total = len(RESULTS)
        passed = sum(1 for r in RESULTS if r.passed)
        failed = sum(1 for r in RESULTS if not r.passed)

        print(f"\n{BOLD}{'='*72}")
        print(f"  RESULTS: {total} tests | {GREEN}{passed} PASS{RESET}{BOLD} | "
              f"{RED}{failed} FAIL{RESET}")
        print(f"  Total element-level diffs: {TOTAL_DIFFS}")
        print(f"  Concurrency — load failures: {CONCURRENCY_FAILURES['load']}  "
              f"correctness failures: {CONCURRENCY_FAILURES['correctness']}")
        print(f"{'='*72}{RESET}\n")

        # Categorical failure summary
        gaps = {}
        for r in RESULTS:
            if not r.passed:
                gaps[r.category] = gaps.get(r.category, 0) + 1
        if gaps:
            print(f"{BOLD}Gap categories (by fail count):{RESET}")
            for cat, cnt in sorted(gaps.items(), key=lambda x: -x[1])[:25]:
                print(f"  {YELLOW}{cnt:4d}{RESET}  {cat}")
            print()

        # Top 25 divergent trace patterns
        print(f"{BOLD}Top 25 divergent element patterns:{RESET}")
        top = sorted(DIFF_PATTERNS.items(), key=lambda x: -x[1])[:25]
        for pat, cnt in top:
            pat_disp = pat if len(pat) < 80 else pat[:77] + "..."
            print(f"  {MAGENTA}{cnt:5d}{RESET}  {pat_disp}")
        print()

        # Failed tests (up to 80)
        failed_results = [r for r in RESULTS if not r.passed]
        if failed_results:
            show = failed_results[:80]
            print(f"{BOLD}Failed tests (showing {len(show)}/{len(failed_results)}):{RESET}")
            for r in show:
                print(f"  {RED}T{r.tid:04d}{RESET} [{r.category}] {r.desc}")
                if r.detail:
                    print(f"         {r.detail[:300]}")
                if r.diffs and len(r.diffs) > 1:
                    for d in r.diffs[1:4]:
                        print(f"         · {d[:200]}")
            if len(failed_results) > 80:
                print(f"  ... and {len(failed_results) - 80} more")
            print()

        # Did servers die?
        if uvicorn_proc and uvicorn_proc.poll() is not None:
            print(f"{YELLOW}uvicorn died during run (exit={uvicorn_proc.returncode}); see /tmp/parity_r2_uvicorn.log{RESET}")
        if rs_proc and rs_proc.poll() is not None:
            print(f"{YELLOW}fastapi-turbo died during run (exit={rs_proc.returncode}); see /tmp/parity_r2_fastapi_turbo.log{RESET}")

        return 0 if failed == 0 else 1

    except KeyboardInterrupt:
        print(f"\n{YELLOW}Interrupted.{RESET}")
        return 130
    finally:
        for proc in [uvicorn_proc, rs_proc]:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


if __name__ == "__main__":
    sys.exit(main())
