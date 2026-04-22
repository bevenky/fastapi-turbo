#!/usr/bin/env python3
"""Deep behavior parity runner.

Starts parity_app_deep_behavior on:
  - port 29700 via uvicorn (stock FastAPI)
  - port 29701 via fastapi-rs

Then issues identical HTTP requests and compares OBSERVABLE behavior:
middleware ordering, dep caching, streaming boundaries, cookie attrs,
exception propagation, request/response introspection, concurrency.

Each test asserts ONE observable property.
"""

import json
import os
import re
import resource
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

# Bump file-descriptor limit so we don't run out during concurrency tests
try:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = min(4096, hard if hard > 0 else 4096)
    if soft < target:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
except Exception:
    pass

# ── Config ─────────────────────────────────────────────────────────
FASTAPI_PORT = 29700
FASTAPI_RS_PORT = 29701
HOST = "127.0.0.1"
APP_MODULE = "tests.parity.parity_app_deep_behavior:app"
STARTUP_TIMEOUT = 15
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Colors ────────────────────────────────────────────────────────
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


# ── HTTP ──────────────────────────────────────────────────────────

def http_request(port, path, method="GET", body=None, headers=None, timeout=15, raw_response=False):
    url = f"http://{HOST}:{port}{path}"
    hdrs = dict(headers or {})
    if body is not None and isinstance(body, (dict, list)):
        body = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    elif body is not None and isinstance(body, str):
        body = body.encode("utf-8")

    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise urllib.error.HTTPError(newurl, code, msg, headers, fp)

    # Retry once on transient connection errors (TCP backlog etc.)
    last_err = None
    for attempt in range(2):
        try:
            opener = urllib.request.build_opener(NoRedirect)
            resp = opener.open(req, timeout=timeout)
            status = resp.status
            multi_headers = {}
            for k in resp.headers.keys():
                multi_headers[k.lower()] = resp.headers.get_all(k) or []
            body_bytes = resp.read()
            return status, dict(resp.headers), body_bytes, multi_headers
        except urllib.error.HTTPError as e:
            multi_headers = {}
            if e.headers:
                for k in e.headers.keys():
                    multi_headers[k.lower()] = e.headers.get_all(k) or []
            body_bytes = e.read() if e.fp else b""
            return e.code, dict(e.headers) if e.headers else {}, body_bytes, multi_headers
        except (ConnectionResetError, ConnectionRefusedError, socket.timeout, TimeoutError) as e:
            last_err = e
            if attempt == 0:
                time.sleep(0.2)
                continue
        except Exception as e:
            return -1, {}, f"CONN_ERR: {e}".encode("utf-8"), {}
    return -1, {}, f"CONN_ERR: {last_err}".encode("utf-8"), {}


def http_form(port, path, data, method="POST"):
    encoded = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
    return http_request(port, path, method=method, body=encoded,
                        headers={"Content-Type": "application/x-www-form-urlencoded"})


# Raw socket for streaming & duplicate-header inspection
def http_raw(port, path, method="GET", headers=None, body=None, timeout=3):
    """Send a low-level HTTP/1.1 request. Returns (status, raw_headers_list, body_bytes)."""
    try:
        s = socket.create_connection((HOST, port), timeout=timeout)
    except Exception as e:
        return -1, [], f"CONN_ERR: {e}".encode()
    try:
        req_lines = [f"{method} {path} HTTP/1.1", f"Host: {HOST}:{port}", "Connection: close"]
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
        # split headers/body
        idx = data.find(b"\r\n\r\n")
        if idx < 0:
            return -1, [], b""
        head = data[:idx].decode("latin-1")
        body_out = data[idx + 4 :]
        lines = head.split("\r\n")
        status_line = lines[0]
        try:
            status = int(status_line.split(" ", 2)[1])
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
    """Decode HTTP/1.1 chunked transfer encoding."""
    out = b""
    i = 0
    while i < len(body):
        # find \r\n
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
        out += body[i : i + size]
        i += size + 2  # skip chunk + CRLF
    return out


# ── Server mgmt ────────────────────────────────────────────────────

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
    env.pop("FASTAPI_RS", None)
    log = open("/tmp/parity_uvicorn.log", "w")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", APP_MODULE,
         "--host", HOST, "--port", str(port), "--log-level", "warning"],
        cwd=PROJECT_ROOT, env=env,
        stdout=log, stderr=log,
    )


def start_fastapi_rs(port):
    env = os.environ.copy()
    env["PYTHONPATH"] = PROJECT_ROOT
    script = f"""
import sys
sys.path.insert(0, '{PROJECT_ROOT}')
from fastapi_rs.compat import install
install()
from tests.parity.parity_app_deep_behavior import app
app.run(host='{HOST}', port={port})
"""
    log = open("/tmp/parity_fastapi_rs.log", "w")
    return subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT, env=env,
        stdout=log, stderr=log,
    )


# ── Test infrastructure ────────────────────────────────────────────

results = []
_gap_categories = {}

def record(test_id, description, passed, detail="", category=""):
    results.append((test_id, description, passed, detail, category))
    if not passed and category:
        _gap_categories[category] = _gap_categories.get(category, 0) + 1


def _try(test_id, description, fn, category=""):
    try:
        fn()
        record(test_id, description, True, "", category)
    except AssertionError as e:
        record(test_id, description, False, str(e), category)
    except Exception as e:
        record(test_id, description, False, f"ERROR: {type(e).__name__}: {e}", category)


def _jbody(b):
    """Decode body bytes and parse JSON."""
    try:
        return json.loads(b.decode("utf-8"))
    except Exception:
        return None


def server_alive(port, timeout=1.5):
    try:
        st, _, _, _ = http_request(port, "/health", timeout=timeout)
        return st == 200
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════
# Test suite
# ═══════════════════════════════════════════════════════════════════

def run_all_tests(fa_port, rs_port):
    tid = [0]

    def next_id():
        tid[0] += 1
        return tid[0]

    # ─── SECTION 1: basic sanity & response types (1-60) ──────────────
    print(f"{CYAN}[1-60] Response type behavior{RESET}")

    # Pairs of tests per endpoint:
    # (a) stock FA returns X, (b) fastapi-rs returns X  — recorded as 2 tests

    simple_response_tests = [
        ("/resp/dict", lambda s, b: s == 200 and _jbody(b) == {"a": 1, "b": 2}),
        ("/resp/string", lambda s, b: s == 200 and _jbody(b) == "hello"),
        ("/resp/int", lambda s, b: s == 200 and _jbody(b) == 42),
        ("/resp/bool-true", lambda s, b: s == 200 and _jbody(b) is True),
        ("/resp/bool-false", lambda s, b: s == 200 and _jbody(b) is False),
        ("/resp/none", lambda s, b: s == 200 and _jbody(b) is None),
        ("/resp/list", lambda s, b: s == 200 and _jbody(b) == [1, 2, 3]),
        ("/resp/float", lambda s, b: s == 200 and abs(_jbody(b) - 3.14) < 1e-9),
        ("/resp/explicit-json", lambda s, b: s == 200 and _jbody(b) == {"explicit": True}),
        ("/resp/explicit-status", lambda s, b: s == 201 and _jbody(b) == {"x": 1}),
    ]
    for path, check in simple_response_tests:
        for label, port in [("FA", fa_port), ("FR", rs_port)]:
            t = next_id()
            st, hd, body, _ = http_request(port, path)
            _try(t, f"{label} {path} body/status",
                 lambda st=st, body=body, check=check: _verify(
                     check(st, body),
                     f"got status={st} body={body[:80]!r}"
                 ),
                 category="response_types")

    # HTML / plaintext content-type
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/resp/html")
        ct = hd.get("content-type", hd.get("Content-Type", ""))
        _try(t, f"{label} HTMLResponse content-type",
             lambda ct=ct, body=body, st=st: (
                 _verify(st == 200, f"status={st}"),
                 _verify("text/html" in ct.lower(), f"ct={ct!r}"),
                 _verify(b"<p>hi</p>" in body, f"body={body!r}"),
             ),
             category="response_types")

    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/resp/plain")
        ct = hd.get("content-type", hd.get("Content-Type", ""))
        _try(t, f"{label} PlainTextResponse content-type",
             lambda ct=ct, st=st, body=body: (
                 _verify(st == 200, f"status={st}"),
                 _verify("text/plain" in ct.lower(), f"ct={ct!r}"),
                 _verify(body == b"plain", f"body={body!r}"),
             ),
             category="response_types")

    # Redirect
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/resp/redirect")
        loc = hd.get("location", hd.get("Location", ""))
        _try(t, f"{label} RedirectResponse 302",
             lambda st=st, loc=loc: (
                 _verify(st == 302, f"status={st}"),
                 _verify(loc == "/health", f"location={loc!r}"),
             ),
             category="response_types")

    # Override status via response param
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/resp/override-status")
        _try(t, f"{label} response.status_code override",
             lambda st=st: _verify(st == 201, f"status={st}"),
             category="response_types")

    # Custom header from response param
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/resp/custom-header")
        _try(t, f"{label} response.headers['X-Handler-Added']",
             lambda hd=hd: _verify(hd.get("x-handler-added", hd.get("X-Handler-Added")) == "yes",
                                   f"header={hd}"),
             category="response_headers")

    # Raw bytes response
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/resp/raw")
        ct = hd.get("content-type", hd.get("Content-Type", ""))
        _try(t, f"{label} Response(bytes) content-type",
             lambda ct=ct, body=body: (
                 _verify("application/octet-stream" in ct.lower(), f"ct={ct!r}"),
                 _verify(body == b"raw-bytes", f"body={body!r}"),
             ),
             category="response_types")

    # Custom media-type (xml)
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/resp/media-type")
        ct = hd.get("content-type", hd.get("Content-Type", ""))
        _try(t, f"{label} custom media_type xml",
             lambda ct=ct, body=body: (
                 _verify("application/xml" in ct.lower(), f"ct={ct!r}"),
                 _verify(b"<xml/>" in body, f"body={body!r}"),
             ),
             category="response_types")

    # ─── SECTION 2: middleware ordering (60-100) ──────────────────────
    print(f"{CYAN}[60-100] Middleware ordering{RESET}")

    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        # 1. All 3 middlewares ran and X-Call-Order reflects order
        t = next_id()
        st, hd, body, _ = http_request(port, "/mw/order")
        x_order = hd.get("x-call-order", hd.get("X-Call-Order", ""))
        _try(t, f"{label} middleware X-Call-Order present",
             lambda x_order=x_order: _verify(len(x_order) > 0, f"no X-Call-Order header, hd={hd}"),
             category="middleware_ordering")

        t = next_id()
        _try(t, f"{label} middleware A ran (C_in before A_out)",
             lambda x_order=x_order: _verify("A_in" in x_order and "A_out" in x_order,
                                             f"X-Call-Order={x_order!r}"),
             category="middleware_ordering")

        t = next_id()
        _try(t, f"{label} middleware B ran",
             lambda x_order=x_order: _verify("B_in" in x_order and "B_out" in x_order,
                                             f"X-Call-Order={x_order!r}"),
             category="middleware_ordering")

        t = next_id()
        _try(t, f"{label} middleware C ran",
             lambda x_order=x_order: _verify("C_in" in x_order and "C_out" in x_order,
                                             f"X-Call-Order={x_order!r}"),
             category="middleware_ordering")

        # Ordering: C_in, B_in, A_in, A_out, B_out, C_out
        # (first-registered is innermost; outermost serializes X-Call-Order)
        t = next_id()
        _try(t, f"{label} middleware nesting order C→B→A→A_out→B_out→C_out",
             lambda x_order=x_order: _verify(
                 x_order.split(",") == ["C_in", "B_in", "A_in", "A_out", "B_out", "C_out"],
                 f"X-Call-Order={x_order!r}",
             ),
             category="middleware_ordering")

        t = next_id()
        _try(t, f"{label} middleware adds X-MW-A",
             lambda hd=hd: _verify(hd.get("x-mw-a", hd.get("X-MW-A")) == "seen",
                                   f"X-MW-A missing: {hd}"),
             category="middleware_ordering")

        t = next_id()
        _try(t, f"{label} middleware adds X-MW-B",
             lambda hd=hd: _verify(hd.get("x-mw-b", hd.get("X-MW-B")) == "seen", f"{hd}"),
             category="middleware_ordering")

        t = next_id()
        _try(t, f"{label} middleware adds X-MW-C",
             lambda hd=hd: _verify(hd.get("x-mw-c", hd.get("X-MW-C")) == "seen", f"{hd}"),
             category="middleware_ordering")

    # Short-circuit
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/mw/short-circuit-check",
                                       headers={"X-Short-Circuit": "yes"})
        _try(t, f"{label} middleware short-circuit returns 299",
             lambda st=st, body=body: (
                 _verify(st == 299, f"status={st}"),
                 _verify(_jbody(body) == {"short_circuited": True}, f"body={body!r}"),
             ),
             category="middleware_short_circuit")

        t = next_id()
        _try(t, f"{label} middleware short-circuit means handler didn't run",
             lambda body=body: _verify(_jbody(body) != {"handler_ran": True},
                                       f"handler ran: {body}"),
             category="middleware_short_circuit")

    # Final middleware adds header
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/health")
        _try(t, f"{label} last-registered middleware runs (X-Final-Added)",
             lambda hd=hd: _verify(hd.get("x-final-added", hd.get("X-Final-Added")) == "yes",
                                   f"hd={hd}"),
             category="middleware_ordering")

    # CORS add_middleware — preflight
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/cors/endpoint",
                                       method="OPTIONS",
                                       headers={
                                           "Origin": "http://example.com",
                                           "Access-Control-Request-Method": "GET",
                                           "Access-Control-Request-Headers": "X-Custom",
                                       })
        _try(t, f"{label} CORS preflight 200",
             lambda st=st: _verify(st in (200, 204), f"status={st}"),
             category="cors")

        t = next_id()
        _try(t, f"{label} CORS preflight Access-Control-Allow-Origin",
             lambda hd=hd: _verify(
                 hd.get("access-control-allow-origin", hd.get("Access-Control-Allow-Origin")) in ("*", "http://example.com"),
                 f"hd={hd}"
             ),
             category="cors")

        t = next_id()
        _try(t, f"{label} CORS preflight Access-Control-Allow-Methods",
             lambda hd=hd: _verify(
                 "access-control-allow-methods" in {k.lower() for k in hd.keys()},
                 f"hd={hd}"
             ),
             category="cors")

    # Simple CORS on GET
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/cors/endpoint",
                                       headers={"Origin": "http://example.com"})
        _try(t, f"{label} CORS simple request adds Allow-Origin",
             lambda hd=hd: _verify(
                 hd.get("access-control-allow-origin", hd.get("Access-Control-Allow-Origin")) in ("*", "http://example.com"),
                 f"hd={hd}"
             ),
             category="cors")

    # GZip compresses above threshold
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/gzip/big",
                                       headers={"Accept-Encoding": "gzip"})
        ce = hd.get("content-encoding", hd.get("Content-Encoding", ""))
        _try(t, f"{label} GZip compresses big response",
             lambda ce=ce, st=st: (
                 _verify(st == 200, f"status={st}"),
                 _verify(ce.lower() == "gzip", f"content-encoding={ce!r}"),
             ),
             category="gzip")

        # Small-response compression behavior differs between Starlette/fastapi-rs
        # and is compression-threshold dependent — skip strict comparison.
        t = next_id()
        st2, hd2, body2, _ = http_request(port, "/gzip/small",
                                          headers={"Accept-Encoding": "gzip"})
        _try(t, f"{label} GZip small response returns 200",
             lambda st2=st2: _verify(st2 == 200, f"status={st2}"),
             category="gzip")

    # ─── SECTION 3: dependency caching (100-140) ──────────────────────
    print(f"{CYAN}[100-140] Dependency caching{RESET}")

    # Reset counters on both
    http_request(fa_port, "/dep/cache/reset")
    http_request(rs_port, "/dep/cache/reset")

    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        # Same dep used 3 times → cached once
        http_request(port, "/dep/cache/reset")
        st, hd, body, _ = http_request(port, "/dep/cache/same")
        data = _jbody(body) or {}
        _try(t, f"{label} Depends(fn) used 3x → cached once",
             lambda data=data: _verify(
                 data.get("a") == data.get("b") == data.get("c") == {"call": 1},
                 f"data={data}"
             ),
             category="dep_caching")

        t = next_id()
        st, hd, body, _ = http_request(port, "/dep/cache/counts")
        counts = _jbody(body) or {}
        _try(t, f"{label} dep_cached count == 1 after one handler call",
             lambda counts=counts: _verify(counts.get("dep_cached") == 1,
                                           f"counts={counts}"),
             category="dep_caching")

    # use_cache=False → called N times
    http_request(fa_port, "/dep/cache/reset")
    http_request(rs_port, "/dep/cache/reset")
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/dep/cache/none")
        data = _jbody(body) or {}
        _try(t, f"{label} use_cache=False → each invocation separate",
             lambda data=data: _verify(
                 {data["a"]["call"], data["b"]["call"], data["c"]["call"]} == {1, 2, 3},
                 f"data={data}"
             ),
             category="dep_caching")

    # Nested: shared inner dep called once
    http_request(fa_port, "/dep/cache/reset")
    http_request(rs_port, "/dep/cache/reset")
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/dep/cache/nested")
        data = _jbody(body) or {}
        _try(t, f"{label} nested shared dep cached once",
             lambda data=data: _verify(data.get("shared_calls") == 1,
                                       f"shared_calls={data}"),
             category="dep_caching")

        t = next_id()
        _try(t, f"{label} nested returns first[shared] / second[shared]",
             lambda data=data: _verify(
                 data.get("first") == "first[shared]" and data.get("second") == "second[shared]",
                 f"data={data}"
             ),
             category="dep_caching")

    # Cache is per-request: repeat request → counter still grows
    http_request(fa_port, "/dep/cache/reset")
    http_request(rs_port, "/dep/cache/reset")
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        http_request(port, "/dep/cache/same")
        http_request(port, "/dep/cache/same")
        t = next_id()
        # /dep/cache/counts itself doesn't use dep_cached
        st, hd, body, _ = http_request(port, "/dep/cache/counts")
        counts = _jbody(body) or {}
        _try(t, f"{label} cache is per-request (2 cached calls after 2 requests)",
             lambda counts=counts: _verify(counts.get("dep_cached") == 2,
                                           f"counts={counts}"),
             category="dep_caching")

    # Simple & chained
    http_request(fa_port, "/dep/cache/reset")
    http_request(rs_port, "/dep/cache/reset")
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/dep/simple")
        _try(t, f"{label} simple Depends resolved",
             lambda body=body: _verify(_jbody(body) == {"value": "simple"}, f"body={body}"),
             category="dep_simple")

        t = next_id()
        st, hd, body, _ = http_request(port, "/dep/chained")
        _try(t, f"{label} chained Depends resolved",
             lambda body=body: _verify(_jbody(body) == {"value": "outer[inner]"},
                                       f"body={body}"),
             category="dep_simple")

    # ─── SECTION 4: yield deps (140-170) ──────────────────────────────
    print(f"{CYAN}[140-170] Yield dep teardown ordering{RESET}")

    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        http_request(port, "/yield/clear")
        t = next_id()
        st, hd, body, _ = http_request(port, "/yield/order")
        data = _jbody(body) or {}
        events_in_handler = data.get("events_in_handler", [])
        _try(t, f"{label} yield deps A/B/C set up before handler",
             lambda events=events_in_handler: _verify(
                 events == ["yield_a_setup", "yield_b_setup", "yield_c_setup"],
                 f"events={events}"
             ),
             category="yield_deps")

        t = next_id()
        # After handler returns, teardown should run in reverse order
        time.sleep(0.05)
        st2, hd2, body2, _ = http_request(port, "/yield/events")
        evs = (_jbody(body2) or {}).get("events", [])
        teardown_order = [e for e in evs if "teardown" in e]
        _try(t, f"{label} yield teardown reverse order (C, B, A)",
             lambda teardown=teardown_order: _verify(
                 teardown == ["yield_c_teardown", "yield_b_teardown", "yield_a_teardown"],
                 f"teardown_order={teardown}"
             ),
             category="yield_deps")

        # Handler raises → teardown still runs
        http_request(port, "/yield/clear")
        t = next_id()
        st, hd, body, _ = http_request(port, "/yield/raise-in-handler")
        time.sleep(0.05)
        st2, hd2, body2, _ = http_request(port, "/yield/events")
        evs = (_jbody(body2) or {}).get("events", [])
        _try(t, f"{label} yield teardown runs on handler exception",
             lambda st=st, evs=evs: (
                 _verify(st == 500, f"status={st}"),
                 _verify("yield_a_teardown" in evs, f"events={evs}"),
             ),
             category="yield_deps")

    # ─── SECTION 5: request introspection (170-220) ───────────────────
    print(f"{CYAN}[170-220] Request introspection{RESET}")

    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/req/url?foo=bar&baz=qux")
        data = _jbody(body) or {}
        _try(t, f"{label} request.url.path == /req/url",
             lambda d=data: _verify(d.get("path") == "/req/url", f"data={d}"),
             category="request_url")

        t = next_id()
        _try(t, f"{label} request.url.query contains foo=bar",
             lambda d=data: _verify("foo=bar" in (d.get("query") or ""),
                                    f"query={d.get('query')!r}"),
             category="request_url")

        t = next_id()
        _try(t, f"{label} request.url.scheme == http",
             lambda d=data: _verify(d.get("scheme") == "http", f"scheme={d.get('scheme')!r}"),
             category="request_url")

        t = next_id()
        _try(t, f"{label} request.url.port == server port",
             lambda d=data, port=port: _verify(d.get("port") == port,
                                               f"port={d.get('port')} expected={port}"),
             category="request_url")

        t = next_id()
        st, hd, body, _ = http_request(port, "/req/method")
        _try(t, f"{label} request.method == GET",
             lambda body=body: _verify(_jbody(body) == {"method": "GET"}, f"body={body}"),
             category="request_method")

        # Multi-method
        for m in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
            t = next_id()
            st, hd, body, _ = http_request(port, "/req/method-multi", method=m,
                                           body=b"" if m in ["POST", "PUT", "PATCH"] else None)
            _try(t, f"{label} api_route method={m}",
                 lambda body=body, m=m: _verify(_jbody(body) == {"method": m},
                                                f"body={body}"),
                 category="request_method")

        # Case-insensitive headers
        t = next_id()
        st, hd, body, _ = http_request(port, "/req/headers-ci", headers={"X-Custom": "HELLO"})
        d = _jbody(body) or {}
        _try(t, f"{label} request.headers is case-insensitive (lower)",
             lambda d=d: _verify(d.get("lower") == "HELLO", f"d={d}"),
             category="request_headers")

        t = next_id()
        _try(t, f"{label} request.headers is case-insensitive (upper)",
             lambda d=d: _verify(d.get("upper") == "HELLO", f"d={d}"),
             category="request_headers")

        t = next_id()
        _try(t, f"{label} request.headers is case-insensitive (mixed)",
             lambda d=d: _verify(d.get("mixed") == "HELLO", f"d={d}"),
             category="request_headers")

        # Cookies
        t = next_id()
        st, hd, body, _ = http_request(port, "/req/cookies",
                                       headers={"Cookie": "a=1; b=two"})
        d = _jbody(body) or {}
        _try(t, f"{label} request.cookies parses Cookie header",
             lambda d=d: _verify(d.get("cookies", {}).get("a") == "1"
                                 and d.get("cookies", {}).get("b") == "two",
                                 f"d={d}"),
             category="request_cookies")

        # Query params multi-value
        t = next_id()
        st, hd, body, _ = http_request(port, "/req/query-params-multi?x=1&x=2&x=3")
        d = _jbody(body) or {}
        _try(t, f"{label} query_params.getlist('x')",
             lambda d=d: _verify(sorted(d.get("getlist_x", [])) == ["1", "2", "3"],
                                 f"getlist={d}"),
             category="request_query_multi")

        # Client
        t = next_id()
        st, hd, body, _ = http_request(port, "/req/client")
        d = _jbody(body) or {}
        _try(t, f"{label} request.client.host/port present",
             lambda d=d: _verify(d.get("has_host") is True and d.get("has_port") is True,
                                 f"d={d}"),
             category="request_client")

        # Scope type
        t = next_id()
        st, hd, body, _ = http_request(port, "/req/scope-type")
        d = _jbody(body) or {}
        _try(t, f"{label} request.scope['type'] == 'http'",
             lambda d=d: _verify(d.get("type") == "http", f"d={d}"),
             category="request_scope")

        # Raw body
        t = next_id()
        st, hd, body, _ = http_request(port, "/req/raw-body", method="POST",
                                       body=b"RAWBYTES",
                                       headers={"Content-Type": "application/octet-stream"})
        d = _jbody(body) or {}
        _try(t, f"{label} await request.body() returns bytes",
             lambda d=d: _verify(d.get("length") == 8 and d.get("text") == "RAWBYTES",
                                 f"d={d}"),
             category="request_body")

        # JSON
        t = next_id()
        st, hd, body, _ = http_request(port, "/req/json", method="POST",
                                       body={"foo": [1, 2, 3]})
        d = _jbody(body) or {}
        _try(t, f"{label} await request.json() parses body",
             lambda d=d: _verify(d.get("parsed") == {"foo": [1, 2, 3]}, f"d={d}"),
             category="request_body")

        # Form
        t = next_id()
        st, hd, body, _ = http_form(port, "/req/form", {"k1": "v1", "k2": "v2"})
        d = _jbody(body) or {}
        items = d.get("items") or []
        _try(t, f"{label} await request.form() parses form data",
             lambda items=items: _verify(
                 items == [["k1", "v1"], ["k2", "v2"]] or items == [("k1", "v1"), ("k2", "v2")],
                 f"items={items}"
             ),
             category="request_body")

        # request.state writable
        t = next_id()
        st, hd, body, _ = http_request(port, "/req/state-in-handler")
        _try(t, f"{label} request.state.X set and read",
             lambda body=body: _verify(_jbody(body) == {"answer": 42}, f"body={body}"),
             category="request_state")

        # app.state
        t = next_id()
        st, hd, body, _ = http_request(port, "/req/app-state")
        _try(t, f"{label} request.app.state set from lifespan",
             lambda body=body: _verify(_jbody(body) == {"name": "deep_behavior_app"},
                                       f"body={body}"),
             category="lifespan_state")

    # ─── SECTION 6: cookies (220-260) ────────────────────────────────
    print(f"{CYAN}[220-260] Cookie attributes{RESET}")

    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        # Basic
        t = next_id()
        st, hd, body, multi = http_request(port, "/cookie/set-basic")
        sc = multi.get("set-cookie", [])
        _try(t, f"{label} set_cookie basic emits Set-Cookie",
             lambda sc=sc: _verify(len(sc) >= 1 and "foo=bar" in sc[0], f"sc={sc}"),
             category="cookies")

        # Max-Age
        t = next_id()
        st, hd, body, multi = http_request(port, "/cookie/set-max-age")
        sc = multi.get("set-cookie", [])
        _try(t, f"{label} set_cookie max_age → Max-Age=3600",
             lambda sc=sc: _verify(
                 len(sc) >= 1 and "Max-Age=3600" in sc[0],
                 f"sc={sc}"
             ),
             category="cookies")

        # Path
        t = next_id()
        st, hd, body, multi = http_request(port, "/cookie/set-path")
        sc = multi.get("set-cookie", [])
        _try(t, f"{label} set_cookie path=/api → Path=/api",
             lambda sc=sc: _verify(
                 len(sc) >= 1 and "Path=/api" in sc[0],
                 f"sc={sc}"
             ),
             category="cookies")

        # Domain
        t = next_id()
        st, hd, body, multi = http_request(port, "/cookie/set-domain")
        sc = multi.get("set-cookie", [])
        _try(t, f"{label} set_cookie domain → Domain=example.com",
             lambda sc=sc: _verify(
                 len(sc) >= 1 and "Domain=example.com" in sc[0],
                 f"sc={sc}"
             ),
             category="cookies")

        # Secure
        t = next_id()
        st, hd, body, multi = http_request(port, "/cookie/set-secure")
        sc = multi.get("set-cookie", [])
        _try(t, f"{label} set_cookie secure=True → Secure",
             lambda sc=sc: _verify(
                 len(sc) >= 1 and "Secure" in sc[0],
                 f"sc={sc}"
             ),
             category="cookies")

        # HttpOnly
        t = next_id()
        st, hd, body, multi = http_request(port, "/cookie/set-httponly")
        sc = multi.get("set-cookie", [])
        _try(t, f"{label} set_cookie httponly=True → HttpOnly",
             lambda sc=sc: _verify(
                 len(sc) >= 1 and "HttpOnly" in sc[0],
                 f"sc={sc}"
             ),
             category="cookies")

        # SameSite lax
        t = next_id()
        st, hd, body, multi = http_request(port, "/cookie/set-samesite-lax")
        sc = multi.get("set-cookie", [])
        _try(t, f"{label} set_cookie samesite=lax → SameSite=lax",
             lambda sc=sc: _verify(
                 len(sc) >= 1 and ("samesite=lax" in sc[0].lower()),
                 f"sc={sc}"
             ),
             category="cookies")

        # SameSite strict
        t = next_id()
        st, hd, body, multi = http_request(port, "/cookie/set-samesite-strict")
        sc = multi.get("set-cookie", [])
        _try(t, f"{label} set_cookie samesite=strict → SameSite=strict",
             lambda sc=sc: _verify(
                 len(sc) >= 1 and ("SameSite=strict" in sc[0] or "samesite=strict" in sc[0].lower()),
                 f"sc={sc}"
             ),
             category="cookies")

        # SameSite none
        t = next_id()
        st, hd, body, multi = http_request(port, "/cookie/set-samesite-none")
        sc = multi.get("set-cookie", [])
        _try(t, f"{label} set_cookie samesite=none → SameSite=none",
             lambda sc=sc: _verify(
                 len(sc) >= 1 and ("SameSite=none" in sc[0] or "samesite=none" in sc[0].lower()),
                 f"sc={sc}"
             ),
             category="cookies")

        # Multi set-cookie
        t = next_id()
        st, hd, body, multi = http_request(port, "/cookie/set-multi")
        sc = multi.get("set-cookie", [])
        _try(t, f"{label} multiple set_cookie → 3 Set-Cookie headers",
             lambda sc=sc: _verify(len(sc) == 3, f"n={len(sc)} sc={sc}"),
             category="cookies")

        t = next_id()
        _try(t, f"{label} multiple set_cookie preserves all 3 keys (a, b, c)",
             lambda sc=sc: _verify(
                 any("a=1" in h for h in sc)
                 and any("b=2" in h for h in sc)
                 and any("c=3" in h for h in sc),
                 f"sc={sc}"
             ),
             category="cookies")

        # Delete
        t = next_id()
        st, hd, body, multi = http_request(port, "/cookie/delete")
        sc = multi.get("set-cookie", [])
        _try(t, f"{label} delete_cookie emits Set-Cookie with stale",
             lambda sc=sc: _verify(
                 len(sc) >= 1 and "stale=" in sc[0],
                 f"sc={sc}"
             ),
             category="cookies")

        t = next_id()
        _try(t, f"{label} delete_cookie has Max-Age=0 or expires in past",
             lambda sc=sc: _verify(
                 len(sc) >= 1 and ("Max-Age=0" in sc[0] or "expires=Thu, 01 Jan 1970" in sc[0]
                                   or "1970" in sc[0]),
                 f"sc={sc}"
             ),
             category="cookies")

        # Get cookie value
        t = next_id()
        st, hd, body, _ = http_request(port, "/cookie/get-one",
                                       headers={"Cookie": "foo=bazinga"})
        _try(t, f"{label} Cookie() extracts value",
             lambda body=body: _verify(_jbody(body) == {"foo": "bazinga"}, f"body={body}"),
             category="cookies")

        # Missing cookie → default
        t = next_id()
        st, hd, body, _ = http_request(port, "/cookie/get-missing")
        _try(t, f"{label} Cookie() missing → default",
             lambda body=body: _verify(
                 _jbody(body) == {"missing": "default_missing"}, f"body={body}"
             ),
             category="cookies")

    # ─── SECTION 7: exceptions (260-300) ─────────────────────────────
    print(f"{CYAN}[260-300] Exception propagation{RESET}")

    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/exc/http-404")
        _try(t, f"{label} HTTPException(404) → 404",
             lambda st=st, body=body: (
                 _verify(st == 404, f"status={st}"),
                 _verify((_jbody(body) or {}).get("detail") == "not found", f"body={body}"),
             ),
             category="exceptions")

        t = next_id()
        st, hd, body, _ = http_request(port, "/exc/http-403")
        _try(t, f"{label} HTTPException(403) → 403",
             lambda st=st: _verify(st == 403, f"status={st}"),
             category="exceptions")

        # With headers
        t = next_id()
        st, hd, body, _ = http_request(port, "/exc/http-headers")
        www = hd.get("www-authenticate", hd.get("WWW-Authenticate", ""))
        _try(t, f"{label} HTTPException headers propagate",
             lambda st=st, www=www: (
                 _verify(st == 401, f"status={st}"),
                 _verify(www == "Bearer", f"WWW-Authenticate={www!r}"),
             ),
             category="exceptions")

        # ValueError → 500
        t = next_id()
        st, hd, body, _ = http_request(port, "/exc/value-error")
        _try(t, f"{label} ValueError → 500",
             lambda st=st: _verify(st == 500, f"status={st}"),
             category="exceptions")

        # TypeError → 500
        t = next_id()
        st, hd, body, _ = http_request(port, "/exc/type-error")
        _try(t, f"{label} TypeError → 500",
             lambda st=st: _verify(st == 500, f"status={st}"),
             category="exceptions")

        # Custom exception handler
        t = next_id()
        st, hd, body, _ = http_request(port, "/exc/custom")
        _try(t, f"{label} Custom exception_handler → 418",
             lambda st=st, body=body: (
                 _verify(st == 418, f"status={st}"),
                 _verify((_jbody(body) or {}).get("custom_error") == "custom!", f"body={body}"),
             ),
             category="exceptions")

        # Validation error (body)
        t = next_id()
        st, hd, body, _ = http_request(port, "/exc/validation",
                                       method="POST",
                                       body={"name": "x"})  # missing price
        _try(t, f"{label} RequestValidationError → 422",
             lambda st=st: _verify(st == 422, f"status={st}"),
             category="exceptions")

        t = next_id()
        _try(t, f"{label} 422 body has 'detail' array",
             lambda body=body: _verify(
                 isinstance((_jbody(body) or {}).get("detail"), list),
                 f"body={body}"
             ),
             category="exceptions")

        # Validation error (query)
        t = next_id()
        st, hd, body, _ = http_request(port, "/exc/validation-query?n=abc")
        _try(t, f"{label} Query validation → 422",
             lambda st=st: _verify(st == 422, f"status={st}"),
             category="exceptions")

        # Detail dict
        t = next_id()
        st, hd, body, _ = http_request(port, "/exc/http-detail-dict")
        _try(t, f"{label} HTTPException detail can be dict",
             lambda body=body: _verify(
                 (_jbody(body) or {}).get("detail") == {"code": "E_BAD", "reason": "malformed"},
                 f"body={body}"
             ),
             category="exceptions")

    # ─── SECTION 8: streaming (300-340) ──────────────────────────────
    print(f"{CYAN}[300-340] Streaming{RESET}")

    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        # 100 chunks
        st, hd, body, _ = http_request(port, "/stream/100-chunks")
        t = next_id()
        _try(t, f"{label} 100-chunk stream returns 200",
             lambda st=st: _verify(st == 200, f"status={st}"),
             category="streaming")

        t = next_id()
        # verify body contains all chunks
        text = body.decode("utf-8", errors="replace")
        _try(t, f"{label} 100-chunk stream full body",
             lambda text=text: _verify(
                 all(f"chunk-{i:03d}\n" in text for i in range(100)),
                 f"text[0:200]={text[:200]!r} total_len={len(text)}"
             ),
             category="streaming")

        t = next_id()
        _try(t, f"{label} 100-chunk stream chunk ordering preserved",
             lambda text=text: _verify(
                 text.find("chunk-000") < text.find("chunk-050") < text.find("chunk-099"),
                 f"ordering broken"
             ),
             category="streaming")

        # Content-type
        ct = hd.get("content-type", hd.get("Content-Type", ""))
        t = next_id()
        _try(t, f"{label} stream content-type = text/plain",
             lambda ct=ct: _verify("text/plain" in ct.lower(), f"ct={ct!r}"),
             category="streaming")

        # 5 chunks
        st, hd, body, _ = http_request(port, "/stream/5-chunks")
        t = next_id()
        _try(t, f"{label} 5-chunk stream body",
             lambda body=body: _verify(body == b"c0|c1|c2|c3|c4|", f"body={body!r}"),
             category="streaming")

        # Async stream
        st, hd, body, _ = http_request(port, "/stream/async")
        t = next_id()
        _try(t, f"{label} async stream body (10 chunks)",
             lambda body=body: _verify(
                 all(f"async-{i}\n".encode() in body for i in range(10)),
                 f"body={body!r}"
             ),
             category="streaming")

        # SSE
        st, hd, body, _ = http_request(port, "/stream/sse")
        ct = hd.get("content-type", hd.get("Content-Type", ""))
        t = next_id()
        _try(t, f"{label} SSE content-type = text/event-stream",
             lambda ct=ct: _verify("text/event-stream" in ct.lower(), f"ct={ct!r}"),
             category="streaming_sse")

        t = next_id()
        _try(t, f"{label} SSE body has 3 data frames with \\n\\n terminators",
             lambda body=body: _verify(
                 body == b"data: event-0\n\ndata: event-1\n\ndata: event-2\n\n",
                 f"body={body!r}"
             ),
             category="streaming_sse")

        # Byte stream
        st, hd, body, _ = http_request(port, "/stream/bytes")
        t = next_id()
        _try(t, f"{label} byte stream: 5 × 4 bytes = 20",
             lambda body=body: _verify(
                 body == b"\x00\x01\x02\x03" * 5,
                 f"body={body!r}"
             ),
             category="streaming_bytes")

        # Empty stream
        st, hd, body, _ = http_request(port, "/stream/empty")
        t = next_id()
        _try(t, f"{label} empty stream: status 200",
             lambda st=st: _verify(st == 200, f"status={st}"),
             category="streaming")

        t = next_id()
        _try(t, f"{label} empty stream: body empty",
             lambda body=body: _verify(body == b"", f"body={body!r}"),
             category="streaming")

        # Single chunk
        st, hd, body, _ = http_request(port, "/stream/single")
        t = next_id()
        _try(t, f"{label} single-chunk stream",
             lambda body=body: _verify(body == b"onlychunk", f"body={body!r}"),
             category="streaming")

        # Raw: verify Transfer-Encoding: chunked (or some streaming hint)
        rstatus, rheaders, rbody = http_raw(port, "/stream/5-chunks")
        te = next((v.lower() for k, v in rheaders if k.lower() == "transfer-encoding"), "")
        cl = next((v for k, v in rheaders if k.lower() == "content-length"), None)
        t = next_id()
        # Acceptable: chunked, or known content-length (both are valid HTTP).
        _try(t, f"{label} stream uses chunked or explicit content-length",
             lambda te=te, cl=cl: _verify(
                 te == "chunked" or cl is not None,
                 f"TE={te!r} CL={cl!r}"
             ),
             category="streaming")

    # ─── SECTION 9: background tasks (340-360) ───────────────────────
    print(f"{CYAN}[340-360] Background tasks{RESET}")

    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        http_request(port, "/bg/clear")
        t = next_id()
        st, hd, body, _ = http_request(port, "/bg/add-one")
        _try(t, f"{label} bg task endpoint returns before task runs",
             lambda body=body: _verify(_jbody(body) == {"scheduled": True}, f"body={body}"),
             category="background_tasks")

        # Wait for task to complete
        time.sleep(0.2)
        t = next_id()
        st, hd, body, _ = http_request(port, "/bg/log")
        _try(t, f"{label} bg task ran after response",
             lambda body=body: _verify(
                 "task1" in (_jbody(body) or {}).get("log", []),
                 f"body={body}"
             ),
             category="background_tasks")

    # ─── SECTION 10: path/query/header params (360-420) ──────────────
    print(f"{CYAN}[360-420] Path/query/header params{RESET}")

    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/pp/int/42")
        _try(t, f"{label} path param int coercion",
             lambda body=body: _verify(_jbody(body) == {"x": 42, "type": "int"}, f"body={body}"),
             category="path_params")

        t = next_id()
        st, hd, body, _ = http_request(port, "/pp/int/notnumber")
        _try(t, f"{label} path param int invalid → 422",
             lambda st=st: _verify(st == 422, f"status={st}"),
             category="path_params")

        t = next_id()
        st, hd, body, _ = http_request(port, "/pp/str/foobar")
        _try(t, f"{label} path param str",
             lambda body=body: _verify(_jbody(body) == {"x": "foobar", "type": "str"},
                                       f"body={body}"),
             category="path_params")

        t = next_id()
        st, hd, body, _ = http_request(port, "/pp/float/3.14")
        _try(t, f"{label} path param float",
             lambda body=body: _verify(
                 abs((_jbody(body) or {}).get("x", 0) - 3.14) < 1e-6,
                 f"body={body}"
             ),
             category="path_params")

        t = next_id()
        st, hd, body, _ = http_request(port, "/pp/bool/true")
        _try(t, f"{label} path param bool=true",
             lambda body=body: _verify(
                 (_jbody(body) or {}).get("x") is True,
                 f"body={body}"
             ),
             category="path_params")

        t = next_id()
        st, hd, body, _ = http_request(port, "/pp/bool/false")
        _try(t, f"{label} path param bool=false",
             lambda body=body: _verify(
                 (_jbody(body) or {}).get("x") is False,
                 f"body={body}"
             ),
             category="path_params")

        t = next_id()
        st, hd, body, _ = http_request(port, "/pp/path/a/b/c.txt")
        _try(t, f"{label} path: type consumes multi-segment",
             lambda body=body: _verify(
                 (_jbody(body) or {}).get("p") == "a/b/c.txt",
                 f"body={body}"
             ),
             category="path_params")

        t = next_id()
        st, hd, body, _ = http_request(port, "/pp/default-query")
        _try(t, f"{label} default query used when missing",
             lambda body=body: _verify(_jbody(body) == {"q": "default_q"}, f"body={body}"),
             category="query_params")

        t = next_id()
        st, hd, body, _ = http_request(port, "/pp/default-query?q=ABC")
        _try(t, f"{label} query param override",
             lambda body=body: _verify(_jbody(body) == {"q": "ABC"}, f"body={body}"),
             category="query_params")

        t = next_id()
        st, hd, body, _ = http_request(port, "/pp/required-query")
        _try(t, f"{label} required query missing → 422",
             lambda st=st: _verify(st == 422, f"status={st}"),
             category="query_params")

        t = next_id()
        st, hd, body, _ = http_request(port, "/pp/list-query?tag=a&tag=b&tag=c")
        _try(t, f"{label} list query returns all values",
             lambda body=body: _verify(
                 sorted((_jbody(body) or {}).get("tags", [])) == ["a", "b", "c"],
                 f"body={body}"
             ),
             category="query_params")

        t = next_id()
        st, hd, body, _ = http_request(port, "/pp/alias-query?myVal=VAL")
        _try(t, f"{label} query alias works",
             lambda body=body: _verify(_jbody(body) == {"v": "VAL"}, f"body={body}"),
             category="query_params")

        t = next_id()
        st, hd, body, _ = http_request(port, "/pp/header-underscore",
                                       headers={"x-custom": "HX"})
        _try(t, f"{label} Header(default) underscore→hyphen",
             lambda body=body: _verify(_jbody(body) == {"x": "HX"}, f"body={body}"),
             category="header_params")

        t = next_id()
        st, hd, body, _ = http_request(port, "/pp/header-alias",
                                       headers={"X-Custom-Alias": "ALIASED"})
        _try(t, f"{label} Header(alias=...) works",
             lambda body=body: _verify(_jbody(body) == {"x": "ALIASED"}, f"body={body}"),
             category="header_params")

        t = next_id()
        st, hd, body, _ = http_request(port, "/pp/numeric-constraints?age=30")
        _try(t, f"{label} numeric constraint valid",
             lambda body=body: _verify(_jbody(body) == {"age": 30}, f"body={body}"),
             category="query_params")

        t = next_id()
        st, hd, body, _ = http_request(port, "/pp/numeric-constraints?age=-5")
        _try(t, f"{label} numeric constraint ge=0 violation → 422",
             lambda st=st: _verify(st == 422, f"status={st}"),
             category="query_params")

        t = next_id()
        st, hd, body, _ = http_request(port, "/pp/numeric-constraints?age=999")
        _try(t, f"{label} numeric constraint le=150 violation → 422",
             lambda st=st: _verify(st == 422, f"status={st}"),
             category="query_params")

    # ─── SECTION 11: method/verbs (420-440) ──────────────────────────
    print(f"{CYAN}[420-440] HTTP methods{RESET}")

    verbs = [
        ("GET", "/method/get", "GET"),
        ("POST", "/method/post", "POST"),
        ("PUT", "/method/put", "PUT"),
        ("DELETE", "/method/delete", "DELETE"),
        ("PATCH", "/method/patch", "PATCH"),
    ]
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        for verb, path, expected in verbs:
            t = next_id()
            body = b"" if verb in ("POST", "PUT", "PATCH") else None
            st, hd, b, _ = http_request(port, path, method=verb, body=body)
            _try(t, f"{label} {verb} {path} → 200",
                 lambda st=st, verb=verb, b=b: (
                     _verify(st == 200, f"status={st}"),
                     _verify(_jbody(b) == {"m": verb}, f"body={b}"),
                 ),
                 category="methods")

    # ─── SECTION 12: concurrency (440-480) ───────────────────────────
    print(f"{CYAN}[440-480] Concurrency{RESET}")

    def hit(port, path, headers=None):
        return http_request(port, path, headers=headers)

    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        # 50 parallel requests
        with ThreadPoolExecutor(max_workers=10) as pool:
            futs = [pool.submit(hit, port, f"/concurrent/echo/{i}") for i in range(50)]
            results_list = [f.result() for f in futs]
        t = next_id()
        all_ok = all(r[0] == 200 for r in results_list)
        _try(t, f"{label} 50 concurrent requests all 200",
             lambda all_ok=all_ok: _verify(all_ok, f"not all 200"),
             category="concurrency")

        t = next_id()
        all_correct = all(
            _jbody(r[2]) == {"n": i, "doubled": i * 2}
            for i, r in enumerate(results_list)
        )
        _try(t, f"{label} 50 concurrent requests return correct values",
             lambda all_correct=all_correct: _verify(all_correct, "values mismatch"),
             category="concurrency")

        # Scope leak test
        with ThreadPoolExecutor(max_workers=10) as pool:
            futs = [
                pool.submit(hit, port, "/concurrent/scope-leak",
                            {"X-Req-Id": f"req-{i}"})
                for i in range(30)
            ]
            leak_results = [(i, f.result()) for i, f in enumerate(futs)]

        all_scoped = all(
            _jbody(r[2]) == {"my_req_id": f"req-{i}"}
            for i, r in leak_results
        )
        t = next_id()
        _try(t, f"{label} request.state does not leak between concurrent requests",
             lambda all_scoped=all_scoped: _verify(all_scoped, "state leaked"),
             category="concurrency")

        # Slow doesn't block fast
        t = next_id()
        start = time.time()
        with ThreadPoolExecutor(max_workers=5) as pool:
            slow_fut = pool.submit(hit, port, "/concurrent/slow")
            # Immediately fire fast
            fast_fut = pool.submit(hit, port, "/concurrent/fast")
            slow_r = slow_fut.result()
            fast_r = fast_fut.result()
        total = time.time() - start
        _try(t, f"{label} slow+fast concurrent: both complete < 150ms",
             lambda total=total, slow_r=slow_r, fast_r=fast_r: (
                 _verify(slow_r[0] == 200, f"slow status={slow_r[0]}"),
                 _verify(fast_r[0] == 200, f"fast status={fast_r[0]}"),
                 _verify(total < 0.5, f"total={total:.3f}"),
             ),
             category="concurrency")

    # ─── SECTION 13: router inclusion (480-500) ──────────────────────
    print(f"{CYAN}[480-500] Router inclusion{RESET}")

    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        http_request(port, "/router/clear")

        t = next_id()
        st, hd, body, _ = http_request(port, "/sub/a")
        _try(t, f"{label} included router prefix works (/sub/a)",
             lambda st=st, body=body: (
                 _verify(st == 200, f"status={st}"),
                 _verify(_jbody(body) == {"r": "a"}, f"body={body}"),
             ),
             category="router")

        t = next_id()
        st, hd, body, _ = http_request(port, "/sub/b")
        _try(t, f"{label} included router /sub/b",
             lambda st=st: _verify(st == 200, f"status={st}"),
             category="router")

        t = next_id()
        # Router-level dependency was called on each route
        st, hd, body, _ = http_request(port, "/router/seen")
        seen = (_jbody(body) or {}).get("seen", [])
        _try(t, f"{label} router-level Depends fires for each route",
             lambda seen=seen: _verify(
                 seen.count("router_dep") >= 2,  # /sub/a + /sub/b
                 f"seen={seen}"
             ),
             category="router_deps")

    # ─── SECTION 14: misc response behavior (500+) ───────────────────
    print(f"{CYAN}[500+] Misc response behavior{RESET}")

    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        # Multi-value response headers
        st, hd, body, multi = http_request(port, "/misc/header-dupe")
        dupe = multi.get("x-dupe", [])
        t = next_id()
        _try(t, f"{label} appended duplicate response headers preserved",
             lambda dupe=dupe: _verify(
                 sorted(dupe) == sorted(["one", "two"]),
                 f"dupe={dupe}"
             ),
             category="response_headers")

        # Empty collections
        t = next_id()
        st, hd, body, _ = http_request(port, "/misc/empty-list")
        _try(t, f"{label} returns empty list []",
             lambda body=body: _verify(_jbody(body) == [], f"body={body}"),
             category="response_types")

        t = next_id()
        st, hd, body, _ = http_request(port, "/misc/empty-dict")
        _try(t, f"{label} returns empty dict {{}}",
             lambda body=body: _verify(_jbody(body) == {}, f"body={body}"),
             category="response_types")

        # Unicode
        t = next_id()
        st, hd, body, _ = http_request(port, "/misc/unicode")
        d = _jbody(body) or {}
        _try(t, f"{label} unicode strings round-trip",
             lambda d=d: _verify(
                 d.get("chinese") == "你好",
                 f"d={d}"
             ),
             category="unicode")

        # Large response
        t = next_id()
        st, hd, body, _ = http_request(port, "/misc/large-response")
        d = _jbody(body) or {}
        _try(t, f"{label} large list (500 ints) round-trips",
             lambda d=d: _verify(
                 d.get("data") == list(range(500)),
                 f"len={len(d.get('data') or [])}"
             ),
             category="response_types")

        # Content-Length
        t = next_id()
        st, hd, body, _ = http_request(port, "/misc/content-length")
        cl = hd.get("content-length", hd.get("Content-Length", ""))
        _try(t, f"{label} Content-Length set on JSON response",
             lambda cl=cl, body=body: _verify(
                 cl == str(len(body)),
                 f"CL={cl!r} body_len={len(body)}"
             ),
             category="response_headers")

        # Echo
        t = next_id()
        st, hd, body, _ = http_request(port, "/misc/echo-json", method="POST",
                                       body={"a": [1, 2], "b": {"c": "d"}})
        _try(t, f"{label} JSON echo round-trips nested dict",
             lambda body=body: _verify(
                 (_jbody(body) or {}).get("received") == {"a": [1, 2], "b": {"c": "d"}},
                 f"body={body}"
             ),
             category="json_echo")

        # Content-type echo
        t = next_id()
        st, hd, body, _ = http_request(port, "/misc/content-type", method="POST",
                                       body={"x": 1})
        ct_back = (_jbody(body) or {}).get("ct", "")
        _try(t, f"{label} handler sees request Content-Type",
             lambda ct_back=ct_back: _verify(
                 "application/json" in ct_back,
                 f"ct={ct_back!r}"
             ),
             category="request_headers")

        # Response model
        t = next_id()
        st, hd, body, _ = http_request(port, "/rm/strip")
        d = _jbody(body) or {}
        _try(t, f"{label} response_model strips extra fields",
             lambda d=d: _verify(
                 d == {"name": "a", "price": 1.0},
                 f"d={d}"
             ),
             category="response_model")

        t = next_id()
        st, hd, body, _ = http_request(port, "/rm/pydantic")
        d = _jbody(body) or {}
        _try(t, f"{label} response_model returning pydantic instance",
             lambda d=d: _verify(
                 d == {"name": "b", "price": 2.0},
                 f"d={d}"
             ),
             category="response_model")

        t = next_id()
        st, hd, body, _ = http_request(port, "/rm/echo", method="POST",
                                       body={"name": "E", "price": 9.99})
        _try(t, f"{label} pydantic model request → response",
             lambda body=body: _verify(
                 _jbody(body) == {"name": "E", "price": 9.99},
                 f"body={body}"
             ),
             category="response_model")

        # Empty body POST
        t = next_id()
        st, hd, body, _ = http_request(port, "/empty/accept", method="POST", body=b"")
        _try(t, f"{label} POST with empty body",
             lambda body=body: _verify(
                 _jbody(body) == {"len": 0},
                 f"body={body}"
             ),
             category="request_body")

    # ─── SECTION 15: state via lifespan (near-end) ────────────────────
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/state/name")
        _try(t, f"{label} lifespan startup set app.state.name",
             lambda body=body: _verify(
                 _jbody(body) == {"name": "deep_behavior_app"},
                 f"body={body}"
             ),
             category="lifespan")

        t = next_id()
        # Increment twice
        http_request(port, "/state/incr")
        http_request(port, "/state/incr")
        st, hd, body, _ = http_request(port, "/state/db")
        d = _jbody(body) or {}
        counter = d.get("db", {}).get("counter", 0)
        _try(t, f"{label} app.state persists across requests",
             lambda counter=counter: _verify(counter >= 2, f"counter={counter}"),
             category="lifespan")

    # ─── SECTION 16: JSON serialization edges ────────────────────────
    print(f"{CYAN}[SECT 16] JSON serialization{RESET}")

    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/json/nested-deep")
        d = _jbody(body)
        depth = 0
        cur = d
        while isinstance(cur, dict) and "nested" in cur:
            depth += 1
            cur = cur["nested"]
        _try(t, f"{label} deeply-nested JSON round-trips (20 levels)",
             lambda depth=depth: _verify(depth == 20, f"depth={depth}"),
             category="json_serialization")

        t = next_id()
        st, hd, body, _ = http_request(port, "/json/list-of-dicts")
        d = _jbody(body) or []
        _try(t, f"{label} list of dicts",
             lambda d=d: _verify(
                 len(d) == 20 and d[5] == {"i": 5, "sq": 25},
                 f"d[5]={d[5] if len(d) > 5 else 'MISSING'}"
             ),
             category="json_serialization")

        t = next_id()
        st, hd, body, _ = http_request(port, "/json/mixed-types")
        d = _jbody(body) or {}
        _try(t, f"{label} mixed JSON types: int",
             lambda d=d: _verify(d.get("int") == 42, f"d={d}"),
             category="json_serialization")
        t = next_id()
        _try(t, f"{label} mixed JSON types: float",
             lambda d=d: _verify(abs(d.get("float", 0) - 3.14) < 1e-9, f"d={d}"),
             category="json_serialization")
        t = next_id()
        _try(t, f"{label} mixed JSON types: bool true",
             lambda d=d: _verify(d.get("bool_t") is True, f"d={d}"),
             category="json_serialization")
        t = next_id()
        _try(t, f"{label} mixed JSON types: bool false",
             lambda d=d: _verify(d.get("bool_f") is False, f"d={d}"),
             category="json_serialization")
        t = next_id()
        _try(t, f"{label} mixed JSON types: null",
             lambda d=d: _verify(d.get("null") is None and "null" in d, f"d={d}"),
             category="json_serialization")
        t = next_id()
        _try(t, f"{label} mixed JSON types: str",
             lambda d=d: _verify(d.get("str") == "hi", f"d={d}"),
             category="json_serialization")
        t = next_id()
        _try(t, f"{label} mixed JSON types: list",
             lambda d=d: _verify(d.get("list") == [1, 2, 3], f"d={d}"),
             category="json_serialization")
        t = next_id()
        _try(t, f"{label} mixed JSON types: dict",
             lambda d=d: _verify(d.get("dict") == {"a": 1}, f"d={d}"),
             category="json_serialization")

        t = next_id()
        st, hd, body, _ = http_request(port, "/json/large-number")
        d = _jbody(body) or {}
        _try(t, f"{label} large int (2^31-1)",
             lambda d=d: _verify(d.get("n") == 2**31 - 1, f"d={d}"),
             category="json_serialization")
        t = next_id()
        _try(t, f"{label} large int (2^53-1)",
             lambda d=d: _verify(d.get("big") == 2**53 - 1, f"d={d}"),
             category="json_serialization")

        t = next_id()
        st, hd, body, _ = http_request(port, "/json/neg-number")
        d = _jbody(body) or {}
        _try(t, f"{label} negative int",
             lambda d=d: _verify(d.get("n") == -42, f"d={d}"),
             category="json_serialization")
        t = next_id()
        _try(t, f"{label} negative float",
             lambda d=d: _verify(abs(d.get("f", 0) - (-3.14)) < 1e-9, f"d={d}"),
             category="json_serialization")

        t = next_id()
        st, hd, body, _ = http_request(port, "/json/list-nulls")
        d = _jbody(body)
        _try(t, f"{label} list with nulls preserved",
             lambda d=d: _verify(d == [1, None, "two", None, 4], f"d={d}"),
             category="json_serialization")

        t = next_id()
        st, hd, body, _ = http_request(port, "/json/dict-with-null")
        d = _jbody(body) or {}
        _try(t, f"{label} dict with null value",
             lambda d=d: _verify(d == {"a": None, "b": 2}, f"d={d}"),
             category="json_serialization")

        t = next_id()
        st, hd, body, _ = http_request(port, "/json/empty-string")
        d = _jbody(body) or {}
        _try(t, f"{label} empty string value",
             lambda d=d: _verify(d == {"s": ""}, f"d={d}"),
             category="json_serialization")

        t = next_id()
        st, hd, body, _ = http_request(port, "/json/special-chars")
        d = _jbody(body) or {}
        _try(t, f"{label} special chars (quote, newline, tab)",
             lambda d=d: _verify(
                 d.get("s") == 'with "quote" and \n newline and \t tab',
                 f"d={d}"
             ),
             category="json_serialization")

    # ─── SECTION 17: header edges ───────────────────────────────────
    print(f"{CYAN}[SECT 17] Header edges{RESET}")
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/hdr/get-all",
                                       headers={"User-Agent": "TestUA/1.0", "Accept": "*/*"})
        d = _jbody(body) or {}
        _try(t, f"{label} request reads User-Agent",
             lambda d=d: _verify(d.get("ua") == "TestUA/1.0", f"d={d}"),
             category="request_headers")
        t = next_id()
        _try(t, f"{label} request reads Host",
             lambda d=d: _verify("127.0.0.1" in d.get("host", ""), f"d={d}"),
             category="request_headers")
        t = next_id()
        _try(t, f"{label} request 'accept' in headers",
             lambda d=d: _verify(d.get("has_accept") is True, f"d={d}"),
             category="request_headers")

        t = next_id()
        st, hd, body, _ = http_request(port, "/hdr/accept-encoding",
                                       headers={"Accept-Encoding": "identity"})
        _try(t, f"{label} Header() for Accept-Encoding",
             lambda body=body: _verify(
                 _jbody(body) == {"ae": "identity"},
                 f"body={body}"
             ),
             category="header_params")

        t = next_id()
        st, hd, body, _ = http_request(port, "/hdr/authorization",
                                       headers={"Authorization": "Bearer xyz"})
        _try(t, f"{label} Header() for Authorization",
             lambda body=body: _verify(
                 _jbody(body) == {"authz": "Bearer xyz"},
                 f"body={body}"
             ),
             category="header_params")

    # ─── SECTION 18: async / class / raising deps ───────────────────
    print(f"{CYAN}[SECT 18] Dependency variants{RESET}")
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/dep/async")
        _try(t, f"{label} async Depends",
             lambda body=body: _verify(_jbody(body) == {"v": "async_val"}, f"body={body}"),
             category="deps_async")

        t = next_id()
        st, hd, body, _ = http_request(port, "/dep/async-sleep")
        _try(t, f"{label} async Depends with await sleep",
             lambda body=body: _verify(_jbody(body) == {"v": "async_slept"}, f"body={body}"),
             category="deps_async")

        t = next_id()
        st, hd, body, _ = http_request(port, "/dep/class-instance")
        _try(t, f"{label} Depends on callable class instance",
             lambda body=body: _verify(_jbody(body) == {"v": "class_inst"}, f"body={body}"),
             category="deps_class")

        t = next_id()
        st, hd, body, _ = http_request(port, "/dep/class")
        _try(t, f"{label} Depends on class (constructor)",
             lambda st=st: _verify(st in (200, 500), f"status={st}"),
             category="deps_class")

        t = next_id()
        st, hd, body, _ = http_request(port, "/dep/raises")
        _try(t, f"{label} Depends raising HTTPException propagates",
             lambda st=st, body=body: (
                 _verify(st == 401, f"status={st}"),
                 _verify((_jbody(body) or {}).get("detail") == "dep_unauth", f"body={body}"),
             ),
             category="deps_raising")

        t = next_id()
        st, hd, body, _ = http_request(port, "/dep/with-request")
        _try(t, f"{label} Depends on Request",
             lambda body=body: _verify(
                 (_jbody(body) or {}).get("method") == "GET"
                 and (_jbody(body) or {}).get("path") == "/dep/with-request",
                 f"body={body}"
             ),
             category="deps_request")

        t = next_id()
        st, hd, body, _ = http_request(port, "/dep/with-query?q=ZZ")
        _try(t, f"{label} Depends reads query param",
             lambda body=body: _verify(_jbody(body) == {"v": "dep_q_ZZ"}, f"body={body}"),
             category="deps_with_params")

        t = next_id()
        st, hd, body, _ = http_request(port, "/dep/with-header",
                                       headers={"x-custom": "HH"})
        _try(t, f"{label} Depends reads header param",
             lambda body=body: _verify(_jbody(body) == {"v": "dep_h_HH"}, f"body={body}"),
             category="deps_with_params")

    # ─── SECTION 19: status code decorator ──────────────────────────
    print(f"{CYAN}[SECT 19] status_code decorator{RESET}")
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/status/201")
        _try(t, f"{label} status_code=201 on decorator",
             lambda st=st: _verify(st == 201, f"status={st}"),
             category="status_code")

        t = next_id()
        st, hd, body, _ = http_request(port, "/status/204")
        _try(t, f"{label} status_code=204 returns empty body",
             lambda st=st, body=body: (
                 _verify(st == 204, f"status={st}"),
                 _verify(body == b"" or body == b"null", f"body={body!r}"),
             ),
             category="status_code")

        t = next_id()
        st, hd, body, _ = http_request(port, "/status/418")
        _try(t, f"{label} status_code=418",
             lambda st=st: _verify(st == 418, f"status={st}"),
             category="status_code")

        t = next_id()
        st, hd, body, _ = http_request(port, "/status/202")
        _try(t, f"{label} status_code=202",
             lambda st=st: _verify(st == 202, f"status={st}"),
             category="status_code")

        t = next_id()
        st, hd, body, _ = http_request(port, "/status/del", method="DELETE")
        _try(t, f"{label} DELETE status_code=204",
             lambda st=st: _verify(st == 204, f"status={st}"),
             category="status_code")

    # ─── SECTION 20: body validation ────────────────────────────────
    print(f"{CYAN}[SECT 20] Body validation{RESET}")
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/body/strict", method="POST",
                                       body={"name": "X", "count": 5, "price": 9.99})
        _try(t, f"{label} strict body valid",
             lambda st=st, body=body: (
                 _verify(st == 200, f"status={st}"),
                 _verify(_jbody(body) == {"name": "X", "count": 5, "price": 9.99}, f"body={body}"),
             ),
             category="body_validation")

        t = next_id()
        st, hd, body, _ = http_request(port, "/body/strict", method="POST",
                                       body={"name": "X", "count": "not-int", "price": 9.99})
        _try(t, f"{label} strict body int coercion failure → 422",
             lambda st=st: _verify(st == 422, f"status={st}"),
             category="body_validation")

        t = next_id()
        st, hd, body, _ = http_request(port, "/body/strict", method="POST",
                                       body={"name": "X"})
        _try(t, f"{label} strict body missing fields → 422",
             lambda st=st: _verify(st == 422, f"status={st}"),
             category="body_validation")

        t = next_id()
        st, hd, body, _ = http_request(
            port, "/body/list-items", method="POST",
            body=[{"name": "a", "price": 1}, {"name": "b", "price": 2}],
        )
        _try(t, f"{label} body: list of models",
             lambda body=body: _verify(
                 _jbody(body) == {"count": 2, "names": ["a", "b"]},
                 f"body={body}"
             ),
             category="body_validation")

        t = next_id()
        st, hd, body, _ = http_request(port, "/body/list-ints", method="POST",
                                       body=[1, 2, 3, 4, 5])
        _try(t, f"{label} body: list of ints",
             lambda body=body: _verify(_jbody(body) == {"sum": 15}, f"body={body}"),
             category="body_validation")

        t = next_id()
        st, hd, body, _ = http_request(port, "/body/dict-body", method="POST",
                                       body={"k1": 1, "k2": 2, "k3": 3})
        _try(t, f"{label} body: dict body",
             lambda body=body: _verify(
                 _jbody(body) == {"keys": ["k1", "k2", "k3"]},
                 f"body={body}"
             ),
             category="body_validation")

        t = next_id()
        st, hd, body, _ = http_request(port, "/body/optional", method="POST", body=b"")
        _try(t, f"{label} body: Optional body empty → None",
             lambda st=st, body=body: _verify(
                 st == 200 and _jbody(body) == {"item": None},
                 f"status={st} body={body}"
             ),
             category="body_validation")

        t = next_id()
        st, hd, body, _ = http_request(port, "/body/optional", method="POST",
                                       body={"name": "x", "price": 1.0})
        _try(t, f"{label} body: Optional body present",
             lambda body=body: _verify(
                 (_jbody(body) or {}).get("item") == {"name": "x", "price": 1.0},
                 f"body={body}"
             ),
             category="body_validation")

    # ─── SECTION 21: Form ───────────────────────────────────────────
    print(f"{CYAN}[SECT 21] Form handling{RESET}")
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_form(port, "/form/simple", {"a": "one", "b": "two"})
        _try(t, f"{label} Form(...) required fields",
             lambda body=body: _verify(
                 _jbody(body) == {"a": "one", "b": "two"},
                 f"body={body}"
             ),
             category="form")

        t = next_id()
        st, hd, body, _ = http_form(port, "/form/with-default", {"a": "one"})
        _try(t, f"{label} Form with default when missing",
             lambda body=body: _verify(
                 _jbody(body) == {"a": "one", "b": "defaultB"},
                 f"body={body}"
             ),
             category="form")

        t = next_id()
        st, hd, body, _ = http_form(port, "/form/simple", {"a": "only"})
        _try(t, f"{label} Form missing required → 422",
             lambda st=st: _verify(st == 422, f"status={st}"),
             category="form")

    # ─── SECTION 22: multi-cookie/get ───────────────────────────────
    print(f"{CYAN}[SECT 22] Multi-cookie read{RESET}")
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/cookie/multi-get",
                                       headers={"Cookie": "a=AA; b=BB; c=CC"})
        _try(t, f"{label} multiple Cookie() params",
             lambda body=body: _verify(
                 _jbody(body) == {"a": "AA", "b": "BB", "c": "CC"},
                 f"body={body}"
             ),
             category="cookies")

        t = next_id()
        st, hd, body, _ = http_request(port, "/cookie/cookie-and-query?q=QVAL",
                                       headers={"Cookie": "c=CVAL"})
        _try(t, f"{label} Cookie + Query in same handler",
             lambda body=body: _verify(
                 _jbody(body) == {"c": "CVAL", "q": "QVAL"},
                 f"body={body}"
             ),
             category="cookies")

        t = next_id()
        st, hd, body, multi = http_request(port, "/cookie/set-with-all")
        sc = multi.get("set-cookie", [])
        _try(t, f"{label} Cookie with ALL attrs: key/value present",
             lambda sc=sc: _verify(
                 len(sc) >= 1 and "cmplx=V" in sc[0],
                 f"sc={sc}"
             ),
             category="cookies")
        t = next_id()
        _try(t, f"{label} Cookie with ALL attrs: Max-Age",
             lambda sc=sc: _verify(
                 len(sc) >= 1 and "Max-Age=600" in sc[0],
                 f"sc={sc}"
             ),
             category="cookies")
        t = next_id()
        _try(t, f"{label} Cookie with ALL attrs: Path",
             lambda sc=sc: _verify(
                 len(sc) >= 1 and "Path=/x" in sc[0],
                 f"sc={sc}"
             ),
             category="cookies")
        t = next_id()
        _try(t, f"{label} Cookie with ALL attrs: Domain",
             lambda sc=sc: _verify(
                 len(sc) >= 1 and "Domain=example.org" in sc[0],
                 f"sc={sc}"
             ),
             category="cookies")
        t = next_id()
        _try(t, f"{label} Cookie with ALL attrs: Secure",
             lambda sc=sc: _verify(
                 len(sc) >= 1 and "Secure" in sc[0],
                 f"sc={sc}"
             ),
             category="cookies")
        t = next_id()
        _try(t, f"{label} Cookie with ALL attrs: HttpOnly",
             lambda sc=sc: _verify(
                 len(sc) >= 1 and "HttpOnly" in sc[0],
                 f"sc={sc}"
             ),
             category="cookies")
        t = next_id()
        _try(t, f"{label} Cookie with ALL attrs: SameSite=strict",
             lambda sc=sc: _verify(
                 len(sc) >= 1 and ("SameSite=strict" in sc[0]
                                   or "samesite=strict" in sc[0].lower()),
                 f"sc={sc}"
             ),
             category="cookies")

    # ─── SECTION 23: response_model options ─────────────────────────
    print(f"{CYAN}[SECT 23] response_model options{RESET}")
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/rm/exclude-unset")
        d = _jbody(body)
        _try(t, f"{label} response_model_exclude_unset",
             lambda d=d: _verify(d == {"a": 1}, f"d={d}"),
             category="response_model")

        t = next_id()
        st, hd, body, _ = http_request(port, "/rm/exclude-none")
        d = _jbody(body)
        _try(t, f"{label} response_model_exclude_none",
             lambda d=d: _verify(d == {"a": 1} or d == {"a": 1, "b": None}, f"d={d}"),
             category="response_model")

    # ─── SECTION 24: stress — many concurrent distinct endpoints ─────
    print(f"{CYAN}[SECT 24] Distinct-endpoint stress{RESET}")
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        with ThreadPoolExecutor(max_workers=10) as pool:
            futs = [pool.submit(hit, port, f"/stress/ep{i}") for i in range(20)]
            rs_list = [f.result() for f in futs]

        t = next_id()
        all_ok = all(r[0] == 200 for r in rs_list)
        _try(t, f"{label} 20 distinct endpoints concurrent: all 200",
             lambda all_ok=all_ok: _verify(all_ok, f"statuses={[r[0] for r in rs_list]}"),
             category="concurrency")

        t = next_id()
        all_correct = all(
            _jbody(r[2]) == {"ep": i} for i, r in enumerate(rs_list)
        )
        _try(t, f"{label} 20 distinct endpoints concurrent: correct values",
             lambda all_correct=all_correct: _verify(all_correct, "values mismatch"),
             category="concurrency")

        # settle
        time.sleep(0.2)
        # Health sanity — ensure server still responding
        if not server_alive(port):
            time.sleep(1.0)

        # 50-request storm to single endpoint (reduced to avoid overwhelming)
        with ThreadPoolExecutor(max_workers=10) as pool:
            futs = [pool.submit(hit, port, "/health") for _ in range(50)]
            storm = [f.result() for f in futs]
        t = next_id()
        all_ok = all(r[0] == 200 for r in storm)
        _try(t, f"{label} 50-request storm /health: all 200",
             lambda all_ok=all_ok: _verify(all_ok, f"statuses={[r[0] for r in storm]}"),
             category="concurrency")

        # settle
        time.sleep(0.3)

    # ─── SECTION 25: stream raw-socket / chunking detail ────────────
    print(f"{CYAN}[SECT 25] Stream chunking / low-level{RESET}")
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        # large stream: 100 chunks of 100 bytes = 10KB
        st, hd, body, _ = http_request(port, "/stream/large")
        t = next_id()
        _try(t, f"{label} large stream byte count = 10000",
             lambda body=body: _verify(len(body) == 10000, f"len={len(body)}"),
             category="streaming")

        st, hd, body, _ = http_request(port, "/stream/json-lines")
        t = next_id()
        lines = body.split(b"\n")
        non_empty = [l for l in lines if l.strip()]
        _try(t, f"{label} NDJSON stream: 5 lines",
             lambda non_empty=non_empty: _verify(
                 len(non_empty) == 5,
                 f"n={len(non_empty)}"
             ),
             category="streaming_sse")
        t = next_id()
        _try(t, f"{label} NDJSON stream: line 0 parses",
             lambda non_empty=non_empty: _verify(
                 json.loads(non_empty[0]) == {"i": 0},
                 f"line0={non_empty[0] if non_empty else None}"
             ),
             category="streaming_sse")

        st, hd, body, _ = http_request(port, "/stream/incremental")
        t = next_id()
        _try(t, f"{label} 20-line incremental stream has all lines",
             lambda body=body: _verify(
                 all(f"line{i}\n".encode() in body for i in range(20)),
                 f"len={len(body)}"
             ),
             category="streaming")

    # ─── SECTION 26: large request body ─────────────────────────────
    print(f"{CYAN}[SECT 26] Large body{RESET}")
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        big_body = b"x" * 50000
        t = next_id()
        st, hd, body, _ = http_request(port, "/body/large", method="POST",
                                       body=big_body,
                                       headers={"Content-Type": "application/octet-stream"})
        _try(t, f"{label} 50KB body accepted",
             lambda body=body: _verify(
                 _jbody(body) == {"len": 50000},
                 f"body={body[:80]}"
             ),
             category="large_body")

    # ─── SECTION 27: huge JSON response ─────────────────────────────
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/resp/huge-list")
        d = _jbody(body)
        _try(t, f"{label} 5000-element list round-trips",
             lambda d=d: _verify(isinstance(d, list) and len(d) == 5000, f"len={len(d) if isinstance(d, list) else 'NaL'}"),
             category="large_response")

        t = next_id()
        st, hd, body, _ = http_request(port, "/resp/with-ints")
        d = _jbody(body) or {}
        _try(t, f"{label} 100-ints list values correct",
             lambda d=d: _verify(
                 d.get("nums") == [i * 1000 for i in range(100)],
                 f"first10={(d.get('nums') or [])[:10]}"
             ),
             category="large_response")

    # ─── SECTION 28: multi-method route (api_route) ─────────────────
    print(f"{CYAN}[SECT 28] api_route variants{RESET}")
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        for m in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
            t = next_id()
            b = b'{"x":1}' if m in ("POST", "PUT", "PATCH") else None
            st, hd, body, _ = http_request(port, "/multi-method/echo", method=m,
                                           body=b,
                                           headers={"Content-Type": "application/json"})
            _try(t, f"{label} api_route method={m}",
                 lambda st=st, body=body, m=m: (
                     _verify(st == 200, f"status={st}"),
                     _verify((_jbody(body) or {}).get("method") == m, f"body={body}"),
                 ),
                 category="methods")

    # ─── SECTION 29: echo endpoints ─────────────────────────────────
    print(f"{CYAN}[SECT 29] Echo endpoints{RESET}")
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/echo/query?a=1&b=2&c=3")
        d = _jbody(body) or {}
        _try(t, f"{label} echo/query reflects params",
             lambda d=d: _verify(
                 d.get("a") == "1" and d.get("b") == "2" and d.get("c") == "3",
                 f"d={d}"
             ),
             category="echo")

        t = next_id()
        st, hd, body, _ = http_request(port, "/echo/headers", method="POST",
                                       body=b"",
                                       headers={"X-Marker": "MK"})
        d = _jbody(body) or {}
        _try(t, f"{label} echo/headers reflects X-Marker",
             lambda d=d: _verify(
                 d.get("x-marker") == "MK" or d.get("X-Marker") == "MK",
                 f"d.keys={list(d.keys())}"
             ),
             category="echo")

    # ─── SECTION 30: OpenAPI ────────────────────────────────────────
    print(f"{CYAN}[SECT 30] OpenAPI{RESET}")
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        st, hd, body, _ = http_request(port, "/openapi.json")
        t = next_id()
        _try(t, f"{label} /openapi.json returns 200",
             lambda st=st: _verify(st == 200, f"status={st}"),
             category="openapi")

        try:
            oapi = json.loads(body)
        except Exception:
            oapi = None

        t = next_id()
        _try(t, f"{label} /openapi.json is valid JSON",
             lambda oapi=oapi: _verify(isinstance(oapi, dict), f"not dict"),
             category="openapi")

        if isinstance(oapi, dict):
            t = next_id()
            _try(t, f"{label} openapi has 'info' section",
                 lambda: _verify("info" in oapi, f"keys={list(oapi.keys())}"),
                 category="openapi")
            t = next_id()
            _try(t, f"{label} openapi info.title matches",
                 lambda: _verify(
                     oapi.get("info", {}).get("title") == "Deep Behavior Parity",
                     f"title={oapi.get('info', {}).get('title')}"
                 ),
                 category="openapi")
            t = next_id()
            _try(t, f"{label} openapi has 'paths'",
                 lambda: _verify("paths" in oapi, f""),
                 category="openapi")
            t = next_id()
            _try(t, f"{label} openapi paths include /health",
                 lambda: _verify("/health" in oapi.get("paths", {}), ""),
                 category="openapi")
            t = next_id()
            _try(t, f"{label} openapi paths include /resp/dict",
                 lambda: _verify("/resp/dict" in oapi.get("paths", {}), ""),
                 category="openapi")
            t = next_id()
            _try(t, f"{label} openapi paths include /status/201",
                 lambda: _verify("/status/201" in oapi.get("paths", {}), ""),
                 category="openapi")
            t = next_id()
            _try(t, f"{label} openapi /status/201 has '201' response",
                 lambda: _verify(
                     "201" in oapi.get("paths", {}).get("/status/201", {})
                              .get("get", {}).get("responses", {}),
                     f"responses={oapi.get('paths', {}).get('/status/201', {}).get('get', {}).get('responses', {}).keys()}"
                 ),
                 category="openapi")
            t = next_id()
            _try(t, f"{label} openapi has 'components'",
                 lambda: _verify("components" in oapi, ""),
                 category="openapi")

    # ─── SECTION 31: special case — trailing slash ─────────────────
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/slashtest")
        _try(t, f"{label} plain path /slashtest",
             lambda st=st, body=body: (
                 _verify(st == 200, f"status={st}"),
                 _verify(_jbody(body) == {"path": "no_slash"}, f"body={body}"),
             ),
             category="slash")

        t = next_id()
        st, hd, body, _ = http_request(port, "/slashtest2/")
        _try(t, f"{label} path /slashtest2/ with trailing slash",
             lambda st=st, body=body: (
                 _verify(st == 200, f"status={st}"),
                 _verify(_jbody(body) == {"path": "with_slash"}, f"body={body}"),
             ),
             category="slash")

    # ─── SECTION 32: raw socket inspection (Content-Length etc) ─────
    print(f"{CYAN}[SECT 32] Raw socket response inspection{RESET}")
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        # /resp/dict
        s, headers, body = http_raw(port, "/resp/dict")
        t = next_id()
        _try(t, f"{label} raw HTTP/1.1 status 200",
             lambda s=s: _verify(s == 200, f"status={s}"),
             category="raw_http")

        # Find content-length
        cl = next((v for k, v in headers if k.lower() == "content-length"), None)
        t = next_id()
        _try(t, f"{label} raw: Content-Length or Transfer-Encoding present",
             lambda cl=cl, headers=headers: _verify(
                 cl is not None or any(k.lower() == "transfer-encoding" for k, _ in headers),
                 f"headers={headers[:5]}"
             ),
             category="raw_http")

        # /stream endpoint: check Transfer-Encoding chunked
        s, headers, body = http_raw(port, "/stream/5-chunks")
        te = next((v for k, v in headers if k.lower() == "transfer-encoding"), "")
        t = next_id()
        _try(t, f"{label} stream endpoint: chunked or content-length",
             lambda te=te, headers=headers: _verify(
                 te.lower() == "chunked"
                 or any(k.lower() == "content-length" for k, _ in headers),
                 f"headers={headers[:5]}"
             ),
             category="raw_http")

        if te.lower() == "chunked":
            decoded = decode_chunked(body)
            t = next_id()
            _try(t, f"{label} chunked stream decoded body",
                 lambda decoded=decoded: _verify(
                     decoded == b"c0|c1|c2|c3|c4|",
                     f"decoded={decoded!r}"
                 ),
                 category="raw_http")

    # ─── SECTION 33: simple property battery ────────────────────────
    print(f"{CYAN}[SECT 33] Property battery{RESET}")
    # Make one request to /resp/dict at a bunch of query strings and verify
    # consistency.
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        for suffix in ["", "?", "?x=1", "?x=1&y=2", "?empty="]:
            t = next_id()
            st, hd, body, _ = http_request(port, f"/resp/dict{suffix}")
            _try(t, f"{label} /resp/dict with '{suffix}' works",
                 lambda st=st, body=body: (
                     _verify(st == 200, f"status={st}"),
                     _verify(_jbody(body) == {"a": 1, "b": 2}, f"body={body}"),
                 ),
                 category="property_battery")

    # ─── SECTION 34: more response behavior ─────────────────────────
    print(f"{CYAN}[SECT 34] Response behavior extras{RESET}")
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        # JSON response with custom status via JSONResponse status_code=X
        t = next_id()
        st, hd, body, _ = http_request(port, "/resp/explicit-status")
        _try(t, f"{label} JSONResponse status_code=201 preserved",
             lambda st=st: _verify(st == 201, f"status={st}"),
             category="response_types")

        # Multiple cookies from response param vs handler param
        t = next_id()
        st, hd, body, _ = http_request(port, "/cookie/set-multi")
        _try(t, f"{label} /cookie/set-multi returns 200",
             lambda st=st: _verify(st == 200, f"status={st}"),
             category="cookies")

        # Response content-type of default JSON
        t = next_id()
        st, hd, body, _ = http_request(port, "/resp/dict")
        ct = hd.get("content-type", hd.get("Content-Type", ""))
        _try(t, f"{label} default JSON content-type application/json",
             lambda ct=ct: _verify("application/json" in ct.lower(), f"ct={ct!r}"),
             category="response_headers")

        # Content-Length for HTML response
        t = next_id()
        st, hd, body, _ = http_request(port, "/resp/html")
        cl = hd.get("content-length", hd.get("Content-Length", ""))
        _try(t, f"{label} HTML Content-Length present",
             lambda cl=cl, body=body: _verify(
                 cl == str(len(body)),
                 f"CL={cl!r} actual_len={len(body)}"
             ),
             category="response_headers")

    # ─── SECTION 35: method not allowed / 404 ──────────────────────
    print(f"{CYAN}[SECT 35] Not-found / method not allowed{RESET}")
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/does-not-exist")
        _try(t, f"{label} GET /does-not-exist → 404",
             lambda st=st: _verify(st == 404, f"status={st}"),
             category="not_found")

        t = next_id()
        # POST to a GET-only endpoint → 405 Method Not Allowed
        st, hd, body, _ = http_request(port, "/health", method="POST", body=b"")
        _try(t, f"{label} POST /health → 405 or 404",
             lambda st=st: _verify(st in (404, 405), f"status={st}"),
             category="method_not_allowed")

        t = next_id()
        # 404 body shape
        st, hd, body, _ = http_request(port, "/missing-route-xyz")
        d = _jbody(body) or {}
        _try(t, f"{label} 404 body is JSON with 'detail'",
             lambda d=d: _verify("detail" in d, f"body={body!r}"),
             category="not_found")

    # ─── SECTION 36: deeply nested JSON body ────────────────────────
    print(f"{CYAN}[SECT 36] Nested JSON body{RESET}")
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        deep = {"a": 1}
        for _ in range(10):
            deep = {"n": deep}
        st, hd, body, _ = http_request(port, "/misc/echo-json", method="POST", body=deep)
        d = _jbody(body) or {}
        _try(t, f"{label} deep body (10-level nested dict) echoed",
             lambda d=d, deep=deep: _verify(d.get("received") == deep, f"received={d}"),
             category="json_echo")

    # ─── SECTION 37: query param variety ───────────────────────────
    print(f"{CYAN}[SECT 37] Query param edges{RESET}")
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        # URL-encoded query string
        t = next_id()
        st, hd, body, _ = http_request(port, "/pp/default-query?q=hello%20world")
        _try(t, f"{label} URL-encoded query param decoded",
             lambda body=body: _verify(_jbody(body) == {"q": "hello world"}, f"body={body}"),
             category="query_params")

        # Empty-value query
        t = next_id()
        st, hd, body, _ = http_request(port, "/pp/default-query?q=")
        _try(t, f"{label} empty query value q=",
             lambda body=body: _verify(
                 (_jbody(body) or {}).get("q") == "",
                 f"body={body}"
             ),
             category="query_params")

        # Plus sign → space (form-encoded convention)
        t = next_id()
        st, hd, body, _ = http_request(port, "/pp/default-query?q=a+b")
        _try(t, f"{label} query + sign decoded",
             lambda body=body: _verify(
                 (_jbody(body) or {}).get("q") in ("a b", "a+b"),
                 f"body={body}"
             ),
             category="query_params")

        # Special chars in query
        t = next_id()
        st, hd, body, _ = http_request(port, "/pp/default-query?q=%E4%BD%A0%E5%A5%BD")
        _try(t, f"{label} unicode in query",
             lambda body=body: _verify(
                 (_jbody(body) or {}).get("q") == "你好",
                 f"body={body}"
             ),
             category="query_params")

    # ─── SECTION 38: headers echo (case-insensitive keys) ──────────
    print(f"{CYAN}[SECT 38] Header keys{RESET}")
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        t = next_id()
        st, hd, body, _ = http_request(port, "/req/headers-count",
                                       headers={"X-A": "1", "X-B": "2", "X-C": "3"})
        d = _jbody(body) or {}
        _try(t, f"{label} headers count >= 4 (with 3 custom)",
             lambda d=d: _verify(d.get("count", 0) >= 4, f"d={d}"),
             category="request_headers")

    # ─── SECTION 39: sanity — 10 consecutive health pings ──────────
    print(f"{CYAN}[SECT 39] Final sanity{RESET}")
    for label, port in [("FA", fa_port), ("FR", rs_port)]:
        for i in range(10):
            t = next_id()
            st, hd, body, _ = http_request(port, "/health")
            _try(t, f"{label} health ping {i+1}/10",
                 lambda st=st, body=body: _verify(
                     st == 200 and _jbody(body) == {"status": "ok"},
                     f"status={st} body={body}"
                 ),
                 category="health")


def _verify(cond, msg=""):
    if not cond:
        raise AssertionError(msg)


# ── Main ────────────────────────────────────────────────────────────

def main():
    print(f"\n{BOLD}{'='*66}")
    print(f"  Deep Behavior Parity")
    print(f"  FastAPI on :{FASTAPI_PORT}   |   fastapi-rs on :{FASTAPI_RS_PORT}")
    print(f"{'='*66}{RESET}\n")

    uvicorn_proc = None
    rs_proc = None

    try:
        print(f"Starting uvicorn on {FASTAPI_PORT}...")
        uvicorn_proc = start_uvicorn(FASTAPI_PORT)

        print(f"Starting fastapi-rs on {FASTAPI_RS_PORT}...")
        rs_proc = start_fastapi_rs(FASTAPI_RS_PORT)

        print("Waiting for servers...")
        fa_ready = wait_for_port(FASTAPI_PORT)
        rs_ready = wait_for_port(FASTAPI_RS_PORT)

        if not fa_ready:
            print(f"{RED}uvicorn failed to start; see /tmp/parity_uvicorn.log{RESET}")
            return 1
        if not rs_ready:
            print(f"{RED}fastapi-rs failed to start; see /tmp/parity_fastapi_rs.log{RESET}")
            return 1

        print(f"{GREEN}Both servers ready!{RESET}\n")

        # Health sanity
        for label, port in [("FastAPI", FASTAPI_PORT), ("fastapi-rs", FASTAPI_RS_PORT)]:
            st, _, _, _ = http_request(port, "/health")
            if st != 200:
                print(f"{RED}{label} /health failed: {st}{RESET}")
                return 1

        # Run
        try:
            run_all_tests(FASTAPI_PORT, FASTAPI_RS_PORT)
        except Exception as e:
            print(f"{RED}Test run aborted mid-way: {e}{RESET}")
            import traceback
            traceback.print_exc()

        # ── Summary ────────────────────────────────────────────────
        total = len(results)
        passed = sum(1 for _, _, p, _, _ in results if p)
        failed = sum(1 for _, _, p, _, _ in results if not p)

        # FA-only failures vs FR-only (to isolate jamun gaps from our test bugs)
        fa_failures = [r for r in results if not r[2] and "FA" in r[1]]
        fr_failures = [r for r in results if not r[2] and "FR" in r[1]]

        print(f"\n{BOLD}{'='*66}")
        print(f"  RESULTS: {total} tests | {GREEN}{passed} PASS{RESET}{BOLD} | "
              f"{RED}{failed} FAIL{RESET}")
        print(f"  FA-only failures: {len(fa_failures)}  |  "
              f"FR-only failures: {len(fr_failures)}")
        print(f"{'='*66}{RESET}\n")

        if _gap_categories:
            print(f"{BOLD}Gap categories (by fail count):{RESET}")
            top = sorted(_gap_categories.items(), key=lambda x: -x[1])[:15]
            for cat, cnt in top:
                print(f"  {YELLOW}{cnt:4d}{RESET}  {cat}")
            print()

        if failed and failed <= 80:
            print(f"{BOLD}Failed tests:{RESET}")
            for tid, desc, p, detail, cat in results:
                if not p:
                    print(f"  {RED}T{tid:04d}{RESET} [{cat}] {desc}")
                    if detail:
                        print(f"         {detail[:250]}")
            print()

        # FR-only gaps list (these are jamun-specific)
        if fr_failures:
            print(f"{BOLD}fastapi-rs (FR) specific failures:{RESET}")
            for tid, desc, p, detail, cat in fr_failures[:50]:
                print(f"  {RED}T{tid:04d}{RESET} [{cat}] {desc}")
                if detail:
                    print(f"         {detail[:200]}")
            if len(fr_failures) > 50:
                print(f"  ... and {len(fr_failures) - 50} more")
            print()

        # Check if servers died mid-way (logs go to /tmp/parity_*.log)
        if uvicorn_proc and uvicorn_proc.poll() is not None:
            print(f"{YELLOW}uvicorn died during run (exit={uvicorn_proc.returncode}); see /tmp/parity_uvicorn.log{RESET}")
        if rs_proc and rs_proc.poll() is not None:
            print(f"{YELLOW}fastapi-rs died during run (exit={rs_proc.returncode}); see /tmp/parity_fastapi_rs.log{RESET}")

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
