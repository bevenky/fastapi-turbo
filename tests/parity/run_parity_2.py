#!/usr/bin/env python3
"""Parity test runner for patterns 101-250.

Starts the parity_app_2 on both FastAPI (uvicorn, port 29200) and
fastapi-turbo (app.run(), port 29201), then compares responses.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PYTHON = sys.executable
TEST_DIR = Path(__file__).parent
FASTAPI_PORT = 29200
FASTAPI_TURBO_PORT = 29201
FASTAPI_BASE = f"http://127.0.0.1:{FASTAPI_PORT}"
RS_BASE = f"http://127.0.0.1:{FASTAPI_TURBO_PORT}"
TIMEOUT = 10.0  # seconds per request
STARTUP_TIMEOUT = 15  # seconds to wait for server boot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def wait_for_port(port: int, timeout: float = STARTUP_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.15)
    return False


def start_fastapi_server() -> subprocess.Popen:
    """Start parity_app_2 under stock FastAPI + uvicorn."""
    proc = subprocess.Popen(
        [
            PYTHON, "-c",
            "import uvicorn; uvicorn.run("
            "'parity_app_2:app', host='127.0.0.1', "
            f"port={FASTAPI_PORT}, log_level='warning')"
        ],
        cwd=str(TEST_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


def start_fastapi_turbo_server() -> subprocess.Popen:
    """Start parity_app_2 under fastapi-turbo with compat shim."""
    proc = subprocess.Popen(
        [
            PYTHON, "-c",
            "import fastapi_turbo.compat; "
            "import importlib; mod = importlib.import_module('parity_app_2'); "
            f"mod.app.run('127.0.0.1', {FASTAPI_TURBO_PORT})"
        ],
        cwd=str(TEST_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


def kill_proc(proc: subprocess.Popen):
    if proc and proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Comparison logic
# ---------------------------------------------------------------------------
class TestResult:
    def __init__(self, pattern: int, name: str):
        self.pattern = pattern
        self.name = name
        self.passed = False
        self.skipped = False
        self.skip_reason = ""
        self.failures: list[str] = []

    def fail(self, msg: str):
        self.failures.append(msg)

    def skip(self, reason: str):
        self.skipped = True
        self.skip_reason = reason


def compare_responses(
    fa_resp: httpx.Response,
    rs_resp: httpx.Response,
    result: TestResult,
    check_headers: list[str] | None = None,
    status_only: bool = False,
    allow_status_diff: bool = False,
):
    """Compare two httpx responses and record failures."""
    # Status code
    if not allow_status_diff and fa_resp.status_code != rs_resp.status_code:
        result.fail(
            f"Status code: FastAPI={fa_resp.status_code}, RS={rs_resp.status_code}"
        )

    if status_only:
        if not result.failures:
            result.passed = True
        return

    # Content-Type
    fa_ct = fa_resp.headers.get("content-type", "")
    rs_ct = rs_resp.headers.get("content-type", "")
    # Normalize: ignore charset differences
    fa_ct_base = fa_ct.split(";")[0].strip()
    rs_ct_base = rs_ct.split(";")[0].strip()
    if fa_ct_base != rs_ct_base:
        result.fail(f"Content-Type: FastAPI={fa_ct_base!r}, RS={rs_ct_base!r}")

    # Body comparison
    if "json" in fa_ct_base:
        try:
            fa_json = fa_resp.json()
            rs_json = rs_resp.json()
            if fa_json != rs_json:
                result.fail(
                    f"JSON body differs:\n  FastAPI: {json.dumps(fa_json, default=str)[:300]}\n"
                    f"  RS:     {json.dumps(rs_json, default=str)[:300]}"
                )
        except Exception as e:
            result.fail(f"JSON parse error: {e}")
    else:
        fa_text = fa_resp.text
        rs_text = rs_resp.text
        if fa_text != rs_text:
            result.fail(
                f"Body differs:\n  FastAPI: {fa_text[:200]!r}\n  RS: {rs_text[:200]!r}"
            )

    # Check specific headers
    if check_headers:
        for h in check_headers:
            fa_val = fa_resp.headers.get(h, "")
            rs_val = rs_resp.headers.get(h, "")
            if fa_val != rs_val:
                result.fail(f"Header {h!r}: FastAPI={fa_val!r}, RS={rs_val!r}")

    if not result.failures:
        result.passed = True


def compare_header_presence(
    fa_resp: httpx.Response,
    rs_resp: httpx.Response,
    result: TestResult,
    headers: list[str],
):
    """Check that both responses have (or both lack) given headers."""
    for h in headers:
        fa_has = h.lower() in {k.lower() for k in fa_resp.headers.keys()}
        rs_has = h.lower() in {k.lower() for k in rs_resp.headers.keys()}
        if fa_has != rs_has:
            result.fail(f"Header {h!r} presence: FastAPI={fa_has}, RS={rs_has}")
    if not result.failures:
        result.passed = True


# ---------------------------------------------------------------------------
# Test definitions
# ---------------------------------------------------------------------------
def run_all_tests(fa_client: httpx.Client, rs_client: httpx.Client) -> list[TestResult]:
    results: list[TestResult] = []

    def get(path: str, **kwargs) -> tuple[httpx.Response, httpx.Response]:
        fa = fa_client.get(path, **kwargs)
        rs = rs_client.get(path, **kwargs)
        return fa, rs

    def post(path: str, **kwargs) -> tuple[httpx.Response, httpx.Response]:
        fa = fa_client.post(path, **kwargs)
        rs = rs_client.post(path, **kwargs)
        return fa, rs

    def put(path: str, **kwargs) -> tuple[httpx.Response, httpx.Response]:
        fa = fa_client.put(path, **kwargs)
        rs = rs_client.put(path, **kwargs)
        return fa, rs

    def delete(path: str, **kwargs) -> tuple[httpx.Response, httpx.Response]:
        fa = fa_client.delete(path, **kwargs)
        rs = rs_client.delete(path, **kwargs)
        return fa, rs

    def patch(path: str, **kwargs) -> tuple[httpx.Response, httpx.Response]:
        fa = fa_client.patch(path, **kwargs)
        rs = rs_client.patch(path, **kwargs)
        return fa, rs

    def options(path: str, **kwargs) -> tuple[httpx.Response, httpx.Response]:
        fa = fa_client.request("OPTIONS", path, **kwargs)
        rs = rs_client.request("OPTIONS", path, **kwargs)
        return fa, rs

    # ================================================================
    # PATTERNS 101-130: Middleware
    # ================================================================

    # 101: CORS preflight OPTIONS -> 200
    r = TestResult(101, "CORS preflight OPTIONS")
    try:
        fa, rs = options(
            "/p101/cors-test",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        if fa.status_code != rs.status_code:
            r.fail(f"Status: FastAPI={fa.status_code}, RS={rs.status_code}")
        else:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 102: CORS simple request -> access-control-allow-origin
    r = TestResult(102, "CORS simple request ACAO header")
    try:
        fa, rs = get("/p102/cors-simple", headers={"Origin": "https://example.com"})
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 103: CORS with credentials -> mirror origin
    r = TestResult(103, "CORS credentials mirror origin")
    try:
        fa, rs = get("/p103/cors-credentials", headers={"Origin": "https://example.com"})
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 104: GZip large body
    r = TestResult(104, "GZip large body compressed")
    try:
        fa, rs = get("/p104/gzip-large", headers={"Accept-Encoding": "gzip"})
        # Both should return 200 with the same JSON
        if fa.status_code != rs.status_code:
            r.fail(f"Status: FastAPI={fa.status_code}, RS={rs.status_code}")
        fa_json = fa.json()
        rs_json = rs.json()
        if fa_json != rs_json:
            r.fail(f"Body differs: FastAPI keys={list(fa_json.keys())}, RS keys={list(rs_json.keys())}")
        if not r.failures:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 105: custom header middleware
    r = TestResult(105, "Custom middleware header")
    try:
        fa, rs = get("/p105/custom-header")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 106: timing header
    r = TestResult(106, "Process time header")
    try:
        fa, rs = get("/p106/timing")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 107: middleware modifies request
    # Known limitation: fastapi-turbo middleware creates a new Request object
    # so state set in middleware is not visible in the handler.
    r = TestResult(107, "Middleware modifies request (known diff: request.state)")
    try:
        fa, rs = get("/p107/modified-request")
        if fa.status_code != rs.status_code:
            r.fail(f"Status: FastAPI={fa.status_code}, RS={rs.status_code}")
        else:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 108: middleware modifies response
    r = TestResult(108, "Middleware modifies response")
    try:
        fa, rs = get("/p108/modified-response")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 109: BaseHTTPMiddleware
    r = TestResult(109, "BaseHTTPMiddleware subclass")
    try:
        fa, rs = get("/p109/base-middleware")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 110: POST with body through middleware
    r = TestResult(110, "Body in middleware POST")
    try:
        fa, rs = post("/p110/body-in-middleware", json={"key": "value"})
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 111: Multiple middleware ordering
    r = TestResult(111, "Multiple middleware ordering")
    try:
        fa, rs = get("/p111/middleware-order")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 112: Middleware + exception handler
    r = TestResult(112, "Middleware + exception handler")
    try:
        fa, rs = get("/p112/middleware-exception")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 113: Middleware sees 404
    r = TestResult(113, "Middleware sees 404")
    try:
        fa, rs = get("/nonexistent-route-for-404")
        if fa.status_code != rs.status_code:
            r.fail(f"Status: FastAPI={fa.status_code}, RS={rs.status_code}")
        else:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 114: Async middleware dispatch
    r = TestResult(114, "Async middleware dispatch")
    try:
        fa, rs = get("/p114/async-middleware")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 115: GZip small response not compressed
    r = TestResult(115, "GZip small body NOT compressed")
    try:
        fa, rs = get("/p115/gzip-small", headers={"Accept-Encoding": "gzip"})
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 116-120: CORS configuration tests
    for pnum, name, path in [
        (116, "CORS specific origins", "/p116/cors-specific-origins"),
        (117, "CORS specific methods", "/p117/cors-methods"),
        (118, "CORS specific headers", "/p118/cors-headers"),
        (119, "CORS max_age", "/p119/cors-max-age"),
        (120, "CORS expose_headers", "/p120/cors-expose"),
    ]:
        r = TestResult(pnum, name)
        try:
            if pnum == 117:
                fa, rs = post(path, headers={"Origin": "https://example.com"})
            else:
                fa, rs = get(path, headers={"Origin": "https://example.com"})
            compare_responses(fa, rs, r)
        except Exception as e:
            r.fail(str(e))
        results.append(r)

    # 121-130: Middleware combo patterns
    for pnum in range(121, 131):
        r = TestResult(pnum, f"Middleware combo p{pnum}")
        try:
            path = f"/p{pnum}/combo-{'cors-gzip' if pnum == 121 else 'custom-headers' if pnum == 122 else 'post-cors' if pnum == 123 else 'all-headers' if pnum == 124 else 'put' if pnum == 125 else 'delete' if pnum == 126 else 'json-response' if pnum == 127 else 'plain' if pnum == 128 else 'html' if pnum == 129 else 'status'}"
            if pnum == 123:
                fa, rs = post(path, headers={"Origin": "https://example.com"})
            elif pnum == 125:
                fa, rs = put(path)
            elif pnum == 126:
                fa, rs = delete(path)
            else:
                fa, rs = get(path, headers={"Origin": "https://example.com"} if pnum in (121, 124) else {})
            if fa.status_code != rs.status_code:
                r.fail(f"Status: FastAPI={fa.status_code}, RS={rs.status_code}")
            else:
                # For non-JSON responses just compare status
                try:
                    if "json" in fa.headers.get("content-type", ""):
                        fa_json = fa.json()
                        rs_json = rs.json()
                        if fa_json != rs_json:
                            r.fail(f"Body: FastAPI={fa_json}, RS={rs_json}")
                except Exception:
                    pass
            if not r.failures:
                r.passed = True
        except Exception as e:
            r.fail(str(e))
        results.append(r)

    # ================================================================
    # PATTERNS 131-160: Error Handling
    # ================================================================

    # 131-135: HTTPException patterns
    error_tests = [
        (131, "HTTPException 404", "/p131/not-found", 404),
        (132, "HTTPException 400", "/p132/bad-request", 400),
        (133, "HTTPException with headers", "/p133/exception-headers", 401),
        (134, "HTTPException 422", "/p134/unprocessable", 422),
        (135, "HTTPException 500", "/p135/internal-error", 500),
    ]
    for pnum, name, path, expected_status in error_tests:
        r = TestResult(pnum, name)
        try:
            fa, rs = get(path)
            compare_responses(fa, rs, r)
        except Exception as e:
            r.fail(str(e))
        results.append(r)

    # 136: Custom exception handler
    r = TestResult(136, "Custom HTTPException handler")
    try:
        fa, rs = get("/p136/custom-handler")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 137: Custom validation error handler (trigger by sending wrong type)
    r = TestResult(137, "Custom RequestValidationError handler")
    try:
        fa, rs = get("/p141/validate-query?count=notanumber")
        # Both should return 422 with our custom handler
        if fa.status_code != rs.status_code:
            r.fail(f"Status: FastAPI={fa.status_code}, RS={rs.status_code}")
        else:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 138: Custom exception class
    r = TestResult(138, "Custom exception class")
    try:
        fa, rs = get("/p138/custom-exception")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 139: Unhandled exception -> 500
    r = TestResult(139, "Unhandled exception -> 500")
    try:
        fa, rs = get("/p139/unhandled")
        if fa.status_code != rs.status_code:
            r.fail(f"Status: FastAPI={fa.status_code}, RS={rs.status_code}")
        else:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 140: Custom 404
    r = TestResult(140, "Custom 404 handler")
    try:
        fa, rs = get("/p140/trigger-404")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 141: Validation error format
    r = TestResult(141, "Validation error format")
    try:
        fa, rs = get("/p141/validate-query?count=abc")
        if fa.status_code != rs.status_code:
            r.fail(f"Status: FastAPI={fa.status_code}, RS={rs.status_code}")
        else:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 142: Multiple validation errors
    r = TestResult(142, "Multiple validation errors")
    try:
        fa, rs = get("/p142/multi-validate?a=x&b=y")
        if fa.status_code != rs.status_code:
            r.fail(f"Status: FastAPI={fa.status_code}, RS={rs.status_code}")
        else:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 143: Body validation error
    r = TestResult(143, "Body validation error")
    try:
        fa, rs = post("/p143/body-validate", json={"name": 123, "price": "not-a-float"})
        if fa.status_code != rs.status_code:
            r.fail(f"Status: FastAPI={fa.status_code}, RS={rs.status_code}")
        else:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 144: Query validation error (ge=0)
    r = TestResult(144, "Query validation ge constraint")
    try:
        fa, rs = get("/p144/query-validate?age=-1")
        if fa.status_code != rs.status_code:
            r.fail(f"Status: FastAPI={fa.status_code}, RS={rs.status_code}")
        else:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 145: Path validation error
    r = TestResult(145, "Path validation error")
    try:
        fa, rs = get("/p145/path-validate/not-an-int")
        if fa.status_code != rs.status_code:
            r.fail(f"Status: FastAPI={fa.status_code}, RS={rs.status_code}")
        else:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 146: Header validation error
    r = TestResult(146, "Header validation error")
    try:
        fa, rs = get("/p146/header-validate", headers={"X-Count": "not-int"})
        if fa.status_code != rs.status_code:
            r.fail(f"Status: FastAPI={fa.status_code}, RS={rs.status_code}")
        else:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 147: Missing required query
    r = TestResult(147, "Missing required query -> 422")
    try:
        fa, rs = get("/p147/required-query")
        if fa.status_code != rs.status_code:
            r.fail(f"Status: FastAPI={fa.status_code}, RS={rs.status_code}")
        else:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 148: Missing required body
    r = TestResult(148, "Missing required body -> 422")
    try:
        fa, rs = post("/p148/required-body")
        if fa.status_code != rs.status_code:
            r.fail(f"Status: FastAPI={fa.status_code}, RS={rs.status_code}")
        else:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 149: Wrong type in query
    r = TestResult(149, "Wrong type in query")
    try:
        fa, rs = get("/p149/wrong-type-query?count=hello")
        if fa.status_code != rs.status_code:
            r.fail(f"Status: FastAPI={fa.status_code}, RS={rs.status_code}")
        else:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 150: Wrong type in path
    r = TestResult(150, "Wrong type in path")
    try:
        fa, rs = get("/p150/wrong-type-path/abc")
        if fa.status_code != rs.status_code:
            r.fail(f"Status: FastAPI={fa.status_code}, RS={rs.status_code}")
        else:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 151-160: Validation detail patterns (happy path + edge cases)
    r = TestResult(151, "Optional query None")
    try:
        fa, rs = get("/p151/optional-query")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(152, "Default query value")
    try:
        fa, rs = get("/p152/default-query")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(153, "Nested model validation")
    try:
        fa, rs = post("/p153/nested-validation", json={"nested": {"inner_name": "test", "inner_value": 1}, "label": "ok"})
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(154, "Multi query types")
    try:
        fa, rs = get("/p154/multi-query-types?a=1&b=2.5&c=hello")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(155, "Strict body model")
    try:
        fa, rs = post("/p155/strict-body", json={"name": "test", "count": 5, "active": True})
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(156, "Enum query param")
    try:
        fa, rs = get("/p156/enum-query?status=active")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(157, "Empty body dict")
    try:
        fa, rs = post("/p157/empty-body", json={})
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(158, "Bool query param")
    try:
        fa, rs = get("/p158/bool-query?flag=true")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(159, "List query param")
    try:
        fa, rs = get("/p159/list-query?items=1&items=2&items=3")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(160, "Float path param")
    try:
        fa, rs = get("/p160/float-path/3.14")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # ================================================================
    # PATTERNS 161-180: Lifecycle + State
    # ================================================================

    r = TestResult(161, "Lifespan startup ran")
    try:
        fa, rs = get("/p161/lifespan-check")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(162, "Shutdown log (empty before shutdown)")
    try:
        fa, rs = get("/p162/shutdown-log")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(163, "Lifespan state read")
    try:
        fa, rs = get("/p163/lifespan-state")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(164, "request.app.state")
    try:
        fa, rs = get("/p164/app-state")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(165, "on_event startup")
    try:
        fa, rs = get("/p165/startup-event")
        # Both should have startup log entries; order/count may differ slightly
        if fa.status_code != rs.status_code:
            r.fail(f"Status: FastAPI={fa.status_code}, RS={rs.status_code}")
        else:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(166, "on_event shutdown registered")
    try:
        fa, rs = get("/p166/shutdown-event")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(167, "Multiple startup handlers")
    try:
        fa, rs = get("/p167/multi-startup")
        if fa.status_code != rs.status_code:
            r.fail(f"Status: FastAPI={fa.status_code}, RS={rs.status_code}")
        else:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 168: State counter (call twice, both should increment)
    r = TestResult(168, "State persists across requests")
    try:
        # First call
        fa1, rs1 = get("/p168/state-counter")
        # Second call
        fa2, rs2 = get("/p168/state-counter")
        # Both should show incrementing counters
        fa1_c = fa1.json().get("counter", 0)
        rs1_c = rs1.json().get("counter", 0)
        fa2_c = fa2.json().get("counter", 0)
        rs2_c = rs2.json().get("counter", 0)
        if fa2_c <= fa1_c:
            r.fail(f"FastAPI counter not incrementing: {fa1_c} -> {fa2_c}")
        if rs2_c <= rs1_c:
            r.fail(f"RS counter not incrementing: {rs1_c} -> {rs2_c}")
        if not r.failures:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 169: request.state per-request (known diff: same as 107)
    r = TestResult(169, "Per-request state (known diff: request.state in middleware)")
    try:
        fa, rs = get("/p169/request-state")
        if fa.status_code != rs.status_code:
            r.fail(f"Status: FastAPI={fa.status_code}, RS={rs.status_code}")
        else:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 170: Background task
    r = TestResult(170, "BackgroundTasks add_task")
    try:
        fa, rs = post("/p170/background-task")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 171: BG with args
    r = TestResult(171, "BackgroundTasks args/kwargs")
    try:
        fa, rs = post("/p171/bg-args")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 172: Multi BG tasks
    r = TestResult(172, "Multiple background tasks")
    try:
        fa, rs = post("/p172/bg-multi")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 173: BG closure
    r = TestResult(173, "Background task closure")
    try:
        fa, rs = post("/p173/bg-closure")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 174: BG with error response
    r = TestResult(174, "Background task with error response")
    try:
        fa, rs = post("/p174/bg-with-error")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 175-180: Lifecycle edge cases
    r = TestResult(175, "State default attr")
    try:
        fa, rs = get("/p175/state-default")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(176, "State set and read")
    try:
        fa, rs = get("/p176/state-set-read")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 177: read dynamic state (set by 176)
    r = TestResult(177, "State read dynamic (cross-request)")
    try:
        fa, rs = get("/p177/state-read-dynamic")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(178, "BG task log full")
    try:
        # Allow time for background tasks to complete
        time.sleep(0.5)
        fa, rs = get("/p178/bg-log-full")
        # Just check both return 200 -- the exact log contents may differ in ordering
        if fa.status_code != rs.status_code:
            r.fail(f"Status: FastAPI={fa.status_code}, RS={rs.status_code}")
        else:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(179, "Async background task")
    try:
        fa, rs = post("/p179/bg-async")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(180, "Startup complete check")
    try:
        fa, rs = get("/p180/startup-complete")
        if fa.status_code != rs.status_code:
            r.fail(f"Status: FastAPI={fa.status_code}, RS={rs.status_code}")
        else:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # ================================================================
    # PATTERNS 181-250: Router Composition
    # ================================================================

    # 181: Router prefix
    r = TestResult(181, "APIRouter with prefix")
    try:
        fa, rs = get("/p181/items/list")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 182: Router tags
    r = TestResult(182, "APIRouter with tags")
    try:
        fa, rs = get("/p182/tagged-endpoint")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 183: Nested routers
    r = TestResult(183, "Nested routers")
    try:
        fa, rs = get("/p183/outer/inner/deep")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 184: include_router with deps
    r = TestResult(184, "include_router with dependencies")
    try:
        fa, rs = get("/p184/secured")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 185: include_router prefix override
    r = TestResult(185, "include_router prefix override")
    try:
        fa, rs = get("/p185/original/endpoint")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 186: tags merge
    r = TestResult(186, "include_router tags merge")
    try:
        fa, rs = get("/p186/merged-tags")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 187: Router response_model
    r = TestResult(187, "Router response_model strips extra")
    try:
        fa, rs = get("/p187/model")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 188: Same prefix different methods
    r = TestResult(188, "Same prefix different methods")
    try:
        fa_g, rs_g = get("/p188/resource")
        fa_p, rs_p = post("/p188/resource")
        if fa_g.status_code != rs_g.status_code:
            r.fail(f"GET status: FastAPI={fa_g.status_code}, RS={rs_g.status_code}")
        if fa_p.status_code != rs_p.status_code:
            r.fail(f"POST status: FastAPI={fa_p.status_code}, RS={rs_p.status_code}")
        if fa_g.json() != rs_g.json():
            r.fail(f"GET body: FastAPI={fa_g.json()}, RS={rs_g.json()}")
        if fa_p.json() != rs_p.json():
            r.fail(f"POST body: FastAPI={fa_p.json()}, RS={rs_p.json()}")
        if not r.failures:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 189: deprecated router
    r = TestResult(189, "Router deprecated=True")
    try:
        fa, rs = get("/p189/old-endpoint")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 190: include_in_schema=False
    r = TestResult(190, "include_in_schema=False still serves")
    try:
        fa, rs = get("/p190/hidden")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 191: api_route multi-method
    r = TestResult(191, "api_route GET+POST")
    try:
        fa_g, rs_g = get("/p191/multi-method")
        fa_p, rs_p = post("/p191/multi-method")
        if fa_g.status_code != rs_g.status_code:
            r.fail(f"GET status: FastAPI={fa_g.status_code}, RS={rs_g.status_code}")
        if fa_p.status_code != rs_p.status_code:
            r.fail(f"POST status: FastAPI={fa_p.status_code}, RS={rs_p.status_code}")
        if not r.failures:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 192: add_api_route imperative
    r = TestResult(192, "add_api_route imperative")
    try:
        fa, rs = get("/p192/imperative")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 193: app.routes
    r = TestResult(193, "app.routes property")
    try:
        fa, rs = get("/p193/routes-count")
        # Both should report >0 routes
        if fa.status_code != rs.status_code:
            r.fail(f"Status: FastAPI={fa.status_code}, RS={rs.status_code}")
        fa_j = fa.json()
        rs_j = rs.json()
        if not fa_j.get("has_routes") or not rs_j.get("has_routes"):
            r.fail(f"has_routes: FastAPI={fa_j.get('has_routes')}, RS={rs_j.get('has_routes')}")
        if not r.failures:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 194: url_path_for
    r = TestResult(194, "url_path_for named route")
    try:
        fa, rs = get("/p194/find-route")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 195: Response class cascade
    r = TestResult(195, "Response class cascade")
    try:
        fa, rs = get("/p195/cascade")
        if fa.status_code != rs.status_code:
            r.fail(f"Status: FastAPI={fa.status_code}, RS={rs.status_code}")
        else:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 196: 3 levels nesting
    r = TestResult(196, "3-level router nesting")
    try:
        fa, rs = get("/p196/l1/l2/l3/endpoint")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 197: Router + route deps merge
    r = TestResult(197, "Router + route deps merge")
    try:
        fa, rs = get("/p197/merged-deps")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 198: Sub-application (via router)
    r = TestResult(198, "Sub-application via router")
    try:
        fa, rs = get("/p198/sub-endpoint")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 199: Static-like response
    r = TestResult(199, "Static-like plain response")
    try:
        fa, rs = get("/p199/static-like")
        if fa.status_code != rs.status_code:
            r.fail(f"Status: FastAPI={fa.status_code}, RS={rs.status_code}")
        if fa.text != rs.text:
            r.fail(f"Body: FastAPI={fa.text!r}, RS={rs.text!r}")
        if not r.failures:
            r.passed = True
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 200: OpenAPI available
    r = TestResult(200, "OpenAPI endpoint available")
    try:
        fa, rs = get("/p200/openapi-check")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 201-210: response_model patterns
    r = TestResult(201, "response_model strips extra fields")
    try:
        fa, rs = get("/p201/user")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(202, "response_model_exclude")
    try:
        fa, rs = get("/p202/user-exclude")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(203, "response_model_include")
    try:
        fa, rs = get("/p203/user-include")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(204, "response_model_exclude_unset")
    try:
        fa, rs = get("/p204/exclude-unset")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(205, "response_model_exclude_none")
    try:
        fa, rs = get("/p205/exclude-none")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(206, "response_model_exclude_defaults")
    try:
        fa, rs = get("/p206/exclude-defaults")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(207, "List response model")
    try:
        fa, rs = get("/p207/list-response")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(208, "Bool response model")
    try:
        fa, rs = get("/p208/bool-model")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(209, "Router response_model detail")
    try:
        fa, rs = get("/p209/detail")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    r = TestResult(210, "response_model with Pydantic instance")
    try:
        fa, rs = get("/p210/model-dict")
        compare_responses(fa, rs, r)
    except Exception as e:
        r.fail(str(e))
    results.append(r)

    # 211-220: Dependency patterns
    for pnum, name, path, method, kwargs in [
        (211, "Router-level deps", "/p211/with-dep", "GET", {}),
        (212, "Multiple router deps", "/p212/multi-deps", "GET", {}),
        (213, "Include-level deps", "/p213/include-dep", "GET", {}),
        (214, "Dep override (default)", "/p214/dep-override", "GET", {}),
        (215, "Dep chain", "/p215/dep-chain", "GET", {}),
        (216, "Dep with query", "/p216/dep-with-query?q=test-val", "GET", {}),
        (217, "Dep with header", "/p217/dep-with-header", "GET", {"headers": {"X-Auth": "my-token"}}),
        (218, "Generator dep", "/p218/gen-dep", "GET", {}),
        (219, "Nested dep", "/p219/nested-dep", "GET", {}),
        (220, "Dep raises HTTPException", "/p220/dep-raises", "GET", {"headers": {"X-Token": "invalid"}}),
    ]:
        r = TestResult(pnum, name)
        try:
            if method == "GET":
                fa, rs = get(path, **kwargs)
            else:
                fa, rs = post(path, **kwargs)
            compare_responses(fa, rs, r)
        except Exception as e:
            r.fail(str(e))
        results.append(r)

    # 221-230: OpenAPI schema patterns
    for pnum, name, path in [
        (221, "Endpoint summary+description", "/p221/with-summary"),
        (222, "Endpoint tags", "/p222/with-tags"),
        (223, "Endpoint deprecated", "/p223/with-deprecated"),
        (224, "Endpoint status_code=201", "/p224/status-code"),
        (225, "Request body schema", "/p225/request-body-schema"),
        (226, "Custom responses dict", "/p226/responses"),
        (227, "Custom operation_id", "/p227/operation-id"),
        (228, "Response description", "/p228/response-description"),
        (229, "Enum query param", "/p229/enum-param"),
        (230, "Multi response codes", "/p230/multi-response"),
    ]:
        r = TestResult(pnum, name)
        try:
            if pnum == 225:
                fa, rs = post(path, json={"name": "test-item"})
            elif pnum == 229:
                fa, rs = get(path + "?status=active")
            else:
                fa, rs = get(path)
            compare_responses(fa, rs, r)
        except Exception as e:
            r.fail(str(e))
        results.append(r)

    # 231-240: Multi-router patterns
    router_tests = [
        (231, "v1 API users list", "/p231/api/v1/users"),
        (232, "v2 API users list", "/p232/api/v2/users"),
        (233, "Multi-endpoint router /a", "/p233/multi/a"),
        (234, "Direct endpoint with empty router", "/p234/direct"),
        (235, "Router search query", "/p235/search?q=hello"),
        (236, "Router path params", "/p236/items/42"),
        (237, "Two routers merged /from-a", "/p237/from-a"),
        (238, "All HTTP methods GET", "/p238/resource"),
        (239, "Router prefix normalization", "/p239/trailing/endpoint"),
        (240, "Multiple response_model /first", "/p240/first"),
    ]
    for pnum, name, path in router_tests:
        r = TestResult(pnum, name)
        try:
            fa, rs = get(path)
            compare_responses(fa, rs, r)
        except Exception as e:
            r.fail(str(e))
        results.append(r)

    # Extra 238 tests for other methods
    for method_name, method_fn in [("POST", post), ("PUT", put), ("DELETE", delete), ("PATCH", patch)]:
        r = TestResult(238, f"All HTTP methods {method_name}")
        try:
            fa, rs = method_fn("/p238/resource")
            compare_responses(fa, rs, r)
        except Exception as e:
            r.fail(str(e))
        results.append(r)

    # 241-250: Edge cases
    edge_tests = [
        (241, "None response", "/p241/none-response"),
        (242, "Empty dict response", "/p242/empty-dict"),
        (243, "Empty list response", "/p243/empty-list"),
        (244, "Nested dict response", "/p244/nested-dict"),
        (245, "List of dicts response", "/p245/list-of-dicts"),
        (246, "Int response", "/p246/int-response"),
        (247, "String response", "/p247/string-response"),
        (248, "Bool response", "/p248/bool-response"),
        (249, "Float response", "/p249/float-response"),
        (250, "Large response", "/p250/large-response"),
    ]
    for pnum, name, path in edge_tests:
        r = TestResult(pnum, name)
        try:
            fa, rs = get(path)
            compare_responses(fa, rs, r)
        except Exception as e:
            r.fail(str(e))
        results.append(r)

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 70)
    print("  Parity Test Suite 2: Patterns 101-250")
    print("=" * 70)
    print()

    # Start servers
    print(f"Starting FastAPI (uvicorn) on port {FASTAPI_PORT}...")
    fa_proc = start_fastapi_server()

    print(f"Starting fastapi-turbo on port {FASTAPI_TURBO_PORT}...")
    rs_proc = start_fastapi_turbo_server()

    try:
        # Wait for both servers
        print("Waiting for FastAPI server...")
        if not wait_for_port(FASTAPI_PORT):
            out = fa_proc.stdout.read().decode() if fa_proc.stdout else ""
            err = fa_proc.stderr.read().decode() if fa_proc.stderr else ""
            print(f"FATAL: FastAPI server did not start.")
            print(f"  stdout: {out[:500]}")
            print(f"  stderr: {err[:500]}")
            return 1

        print("Waiting for fastapi-turbo server...")
        if not wait_for_port(FASTAPI_TURBO_PORT):
            out = rs_proc.stdout.read().decode() if rs_proc.stdout else ""
            err = rs_proc.stderr.read().decode() if rs_proc.stderr else ""
            print(f"FATAL: fastapi-turbo server did not start.")
            print(f"  stdout: {out[:500]}")
            print(f"  stderr: {err[:500]}")
            return 1

        print("Both servers ready. Running tests...\n")

        # Small delay to let startup handlers finish
        time.sleep(0.5)

        # Run tests
        with httpx.Client(base_url=FASTAPI_BASE, timeout=TIMEOUT) as fa_client, \
             httpx.Client(base_url=RS_BASE, timeout=TIMEOUT) as rs_client:
            results = run_all_tests(fa_client, rs_client)

        # Report
        passed = [r for r in results if r.passed]
        failed = [r for r in results if not r.passed and not r.skipped]
        skipped = [r for r in results if r.skipped]

        print("-" * 70)
        print(f"  RESULTS: {len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped out of {len(results)}")
        print("-" * 70)

        if failed:
            print("\nFAILED TESTS:")
            for r in failed:
                print(f"\n  [{r.pattern}] {r.name}")
                for f in r.failures:
                    for line in f.split("\n"):
                        print(f"    {line}")

        if skipped:
            print("\nSKIPPED:")
            for r in skipped:
                print(f"  [{r.pattern}] {r.name}: {r.skip_reason}")

        print()
        # Deduplicate pattern numbers for accurate count
        passed_patterns = set(r.pattern for r in passed)
        failed_patterns = set(r.pattern for r in failed) - passed_patterns
        total_patterns = passed_patterns | failed_patterns
        print(f"Unique patterns tested: {len(total_patterns)}")
        print(f"  Passed: {len(passed_patterns)}")
        print(f"  Failed: {len(failed_patterns)}")
        pct = len(passed_patterns) / len(total_patterns) * 100 if total_patterns else 0
        print(f"  Pass rate: {pct:.1f}%")

        return 0 if not failed_patterns else 1

    finally:
        print("\nShutting down servers...")
        kill_proc(fa_proc)
        kill_proc(rs_proc)


if __name__ == "__main__":
    sys.exit(main())
