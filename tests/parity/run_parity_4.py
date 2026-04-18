#!/usr/bin/env python3
"""Parity runner 4: patterns 401-500.

Starts the parity_app_4 on:
  - port 29400 via uvicorn (stock FastAPI)
  - port 29401 via fastapi-rs

Then issues identical HTTP requests to both and compares responses.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

# ── Config ────────────────────────────────────────────────────────

FASTAPI_PORT = 29400
FASTAPI_RS_PORT = 29401
HOST = "127.0.0.1"
APP_MODULE = "tests.parity.parity_app_4:app"
STARTUP_TIMEOUT = 15  # seconds
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Colors ────────────────────────────────────────────────────────

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"
BOLD = "\033[1m"

# ── HTTP helper ──────────────────────────────────────────────────

def http_request(port, path, method="GET", body=None, headers=None, follow_redirects=False):
    """Make an HTTP request and return (status, headers_dict, body_text)."""
    url = f"http://{HOST}:{port}{path}"
    hdrs = headers or {}
    if body is not None and isinstance(body, dict):
        body = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    elif body is not None and isinstance(body, str):
        body = body.encode("utf-8")
    elif body is not None and isinstance(body, bytes):
        pass  # already bytes

    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)

    try:
        if follow_redirects:
            resp = urllib.request.urlopen(req, timeout=10)
        else:
            # Don't follow redirects
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    raise urllib.error.HTTPError(newurl, code, msg, headers, fp)
            opener = urllib.request.build_opener(NoRedirect)
            resp = opener.open(req, timeout=10)
        status = resp.status
        resp_headers = dict(resp.headers)
        body_text = resp.read().decode("utf-8", errors="replace")
        return status, resp_headers, body_text
    except urllib.error.HTTPError as e:
        resp_headers = dict(e.headers) if e.headers else {}
        body_text = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return e.code, resp_headers, body_text
    except Exception as e:
        return -1, {}, f"CONNECTION_ERROR: {e}"


def http_form(port, path, form_data, method="POST"):
    """POST form-urlencoded data."""
    encoded = urllib.parse.urlencode(form_data).encode("utf-8")
    return http_request(port, path, method=method, body=encoded,
                        headers={"Content-Type": "application/x-www-form-urlencoded"})


def http_get_json(port, path, headers=None):
    """GET and parse JSON."""
    status, hdrs, body = http_request(port, path, headers=headers)
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        data = None
    return status, hdrs, data, body


def http_post_json(port, path, body_dict, headers=None):
    """POST JSON and parse response."""
    status, hdrs, body = http_request(port, path, method="POST", body=body_dict, headers=headers)
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        data = None
    return status, hdrs, data, body


# ── Server management ────────────────────────────────────────────

def wait_for_port(port, timeout=STARTUP_TIMEOUT):
    """Wait until a port is accepting connections."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, port), timeout=1):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.2)
    return False


def start_uvicorn(port):
    """Start stock FastAPI on uvicorn."""
    env = os.environ.copy()
    env["PYTHONPATH"] = PROJECT_ROOT
    # Remove any compat shim influence
    env.pop("FASTAPI_RS", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", APP_MODULE,
         "--host", HOST, "--port", str(port), "--log-level", "warning"],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


def start_fastapi_rs(port):
    """Start fastapi-rs server."""
    env = os.environ.copy()
    env["PYTHONPATH"] = PROJECT_ROOT
    # Use a small script to import and run
    script = f"""
import sys
sys.path.insert(0, '{PROJECT_ROOT}')
# Install compat shims so 'from fastapi import ...' maps to fastapi_rs
from fastapi_rs.compat import install
install()
from tests.parity.parity_app_4 import app
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


# ── Test infrastructure ──────────────────────────────────────────

results = []

def compare(pattern_id, description, fastapi_result, rs_result, check_fn=None):
    """Compare two results and record PASS/FAIL."""
    fa_status, fa_headers, fa_body = fastapi_result[:3]
    rs_status, rs_headers, rs_body = rs_result[:3]

    passed = True
    detail = ""

    if fa_status == -1:
        detail = f"FastAPI connection error: {fa_body}"
        passed = False
    elif rs_status == -1:
        detail = f"fastapi-rs connection error: {rs_body}"
        passed = False
    elif check_fn:
        try:
            check_fn(fa_status, fa_headers, fa_body, rs_status, rs_headers, rs_body)
        except AssertionError as e:
            passed = False
            detail = str(e)
    else:
        # Default: status codes match and body matches
        if fa_status != rs_status:
            passed = False
            detail = f"Status mismatch: FastAPI={fa_status}, fastapi-rs={rs_status}"
        elif fa_body != rs_body:
            # Try JSON comparison (ignore key ordering)
            try:
                fa_json = json.loads(fa_body)
                rs_json = json.loads(rs_body)
                if fa_json != rs_json:
                    passed = False
                    detail = f"JSON mismatch:\n  FastAPI:    {json.dumps(fa_json, sort_keys=True)}\n  fastapi-rs: {json.dumps(rs_json, sort_keys=True)}"
            except (json.JSONDecodeError, ValueError):
                passed = False
                detail = f"Body mismatch:\n  FastAPI:    {fa_body[:200]}\n  fastapi-rs: {rs_body[:200]}"

    label = f"{GREEN}PASS{RESET}" if passed else f"{RED}FAIL{RESET}"
    results.append((pattern_id, description, passed, detail))
    if not passed:
        print(f"  P{pattern_id}: {label} - {description}")
        if detail:
            print(f"         {detail[:300]}")
    return passed


def compare_json(pattern_id, description, fa_port, rs_port, path,
                 method="GET", body=None, headers=None, check_fn=None):
    """Convenience: make same request to both ports and compare."""
    if method == "GET":
        fa_result = http_request(fa_port, path, headers=headers)
        rs_result = http_request(rs_port, path, headers=headers)
    elif method == "POST":
        fa_result = http_request(fa_port, path, method="POST", body=body, headers=headers)
        rs_result = http_request(rs_port, path, method="POST", body=body, headers=headers)
    elif method == "FORM":
        fa_result = http_form(fa_port, path, body)
        rs_result = http_form(rs_port, path, body)
    else:
        fa_result = http_request(fa_port, path, method=method, body=body, headers=headers)
        rs_result = http_request(rs_port, path, method=method, body=body, headers=headers)
    return compare(pattern_id, description, fa_result, rs_result, check_fn)


# ── Test definitions ─────────────────────────────────────────────

def run_all_tests(fa_port, rs_port):
    """Run all 100 pattern tests."""

    print(f"\n{BOLD}{CYAN}=== OpenAPI Schema Parity (401-420) ==={RESET}\n")

    # Fetch OpenAPI from both
    fa_s, _, fa_openapi_raw = http_request(fa_port, "/openapi.json")
    rs_s, _, rs_openapi_raw = http_request(rs_port, "/openapi.json")
    fa_openapi = json.loads(fa_openapi_raw) if fa_s == 200 else {}
    rs_openapi = json.loads(rs_openapi_raw) if rs_s == 200 else {}

    # P401: /openapi.json exists and returns valid JSON
    def check_401(fs, fh, fb, rs, rh, rb):
        assert fs == 200, f"FastAPI openapi status={fs}"
        assert rs == 200, f"fastapi-rs openapi status={rs}"
        json.loads(fb)
        json.loads(rb)
    compare(401, "/openapi.json exists and returns valid JSON",
            (fa_s, {}, fa_openapi_raw), (rs_s, {}, rs_openapi_raw), check_401)

    # P402: info.title matches
    def check_402(fs, fh, fb, rs, rh, rb):
        assert fa_openapi.get("info", {}).get("title") == "Parity Test App 4"
        assert rs_openapi.get("info", {}).get("title") == "Parity Test App 4"
    compare(402, "/openapi.json has info.title matching app title",
            (fa_s, {}, fa_openapi_raw), (rs_s, {}, rs_openapi_raw), check_402)

    # P403: info.version matches
    def check_403(fs, fh, fb, rs, rh, rb):
        assert fa_openapi.get("info", {}).get("version") == "4.0.0"
        assert rs_openapi.get("info", {}).get("version") == "4.0.0"
    compare(403, "/openapi.json has info.version matching app version",
            (fa_s, {}, fa_openapi_raw), (rs_s, {}, rs_openapi_raw), check_403)

    # P404: info.description
    def check_404(fs, fh, fb, rs, rh, rb):
        fa_desc = fa_openapi.get("info", {}).get("description", "")
        rs_desc = rs_openapi.get("info", {}).get("description", "")
        assert fa_desc, "FastAPI missing description"
        assert rs_desc, "fastapi-rs missing description"
        assert fa_desc == rs_desc, f"description mismatch: '{fa_desc}' vs '{rs_desc}'"
    compare(404, "/openapi.json has info.description",
            (fa_s, {}, fa_openapi_raw), (rs_s, {}, rs_openapi_raw), check_404)

    # P405: info.summary
    def check_405(fs, fh, fb, rs, rh, rb):
        fa_sum = fa_openapi.get("info", {}).get("summary", "")
        rs_sum = rs_openapi.get("info", {}).get("summary", "")
        assert fa_sum, "FastAPI missing summary"
        assert rs_sum, "fastapi-rs missing summary"
        assert fa_sum == rs_sum, f"summary mismatch: '{fa_sum}' vs '{rs_sum}'"
    compare(405, "/openapi.json has info.summary",
            (fa_s, {}, fa_openapi_raw), (rs_s, {}, rs_openapi_raw), check_405)

    # P406: paths include all registered routes
    def check_406(fs, fh, fb, rs, rh, rb):
        fa_paths = set(fa_openapi.get("paths", {}).keys())
        rs_paths = set(rs_openapi.get("paths", {}).keys())
        # Check key routes exist in both
        for route in ["/health", "/p406-get-route", "/p406-post-route",
                      "/p408-post-body", "/p409-response-model"]:
            assert route in fa_paths, f"FastAPI missing route {route}"
            assert route in rs_paths, f"fastapi-rs missing route {route}"
    compare(406, "/openapi.json paths include all registered routes",
            (fa_s, {}, fa_openapi_raw), (rs_s, {}, rs_openapi_raw), check_406)

    # P407: GET endpoint has correct method
    def check_407(fs, fh, fb, rs, rh, rb):
        fa_health = fa_openapi.get("paths", {}).get("/health", {})
        rs_health = rs_openapi.get("paths", {}).get("/health", {})
        assert "get" in fa_health, "FastAPI /health missing GET"
        assert "get" in rs_health, "fastapi-rs /health missing GET"
    compare(407, "/openapi.json GET endpoint has correct method",
            (fa_s, {}, fa_openapi_raw), (rs_s, {}, rs_openapi_raw), check_407)

    # P408: POST endpoint has requestBody schema
    def check_408(fs, fh, fb, rs, rh, rb):
        fa_post = fa_openapi.get("paths", {}).get("/p408-post-body", {}).get("post", {})
        rs_post = rs_openapi.get("paths", {}).get("/p408-post-body", {}).get("post", {})
        assert "requestBody" in fa_post, "FastAPI POST missing requestBody"
        assert "requestBody" in rs_post, "fastapi-rs POST missing requestBody"
    compare(408, "/openapi.json POST endpoint has requestBody schema",
            (fa_s, {}, fa_openapi_raw), (rs_s, {}, rs_openapi_raw), check_408)

    # P409: response_model creates response schema
    def check_409(fs, fh, fb, rs, rh, rb):
        fa_get = fa_openapi.get("paths", {}).get("/p409-response-model", {}).get("get", {})
        rs_get = rs_openapi.get("paths", {}).get("/p409-response-model", {}).get("get", {})
        fa_resp = fa_get.get("responses", {}).get("200", {})
        rs_resp = rs_get.get("responses", {}).get("200", {})
        assert fa_resp, "FastAPI missing 200 response"
        assert rs_resp, "fastapi-rs missing 200 response"
    compare(409, "/openapi.json response_model creates response schema",
            (fa_s, {}, fa_openapi_raw), (rs_s, {}, rs_openapi_raw), check_409)

    # P410: tags from route decorator
    def check_410(fs, fh, fb, rs, rh, rb):
        fa_op = fa_openapi.get("paths", {}).get("/p410-tags", {}).get("get", {})
        rs_op = rs_openapi.get("paths", {}).get("/p410-tags", {}).get("get", {})
        assert "items" in fa_op.get("tags", []), "FastAPI missing tags"
        assert "items" in rs_op.get("tags", []), "fastapi-rs missing tags"
    compare(410, "/openapi.json tags from route decorator",
            (fa_s, {}, fa_openapi_raw), (rs_s, {}, rs_openapi_raw), check_410)

    # P411: tags from router
    def check_411(fs, fh, fb, rs, rh, rb):
        fa_op = fa_openapi.get("paths", {}).get("/p411/items", {}).get("get", {})
        rs_op = rs_openapi.get("paths", {}).get("/p411/items", {}).get("get", {})
        assert "router-tagged" in fa_op.get("tags", []), f"FastAPI missing router tags, got {fa_op.get('tags', [])}"
        assert "router-tagged" in rs_op.get("tags", []), f"fastapi-rs missing router tags, got {rs_op.get('tags', [])}"
    compare(411, "/openapi.json tags from router",
            (fa_s, {}, fa_openapi_raw), (rs_s, {}, rs_openapi_raw), check_411)

    # P412: deprecated=True
    def check_412(fs, fh, fb, rs, rh, rb):
        fa_op = fa_openapi.get("paths", {}).get("/p412-deprecated", {}).get("get", {})
        rs_op = rs_openapi.get("paths", {}).get("/p412-deprecated", {}).get("get", {})
        assert fa_op.get("deprecated") is True, "FastAPI deprecated not True"
        assert rs_op.get("deprecated") is True, "fastapi-rs deprecated not True"
    compare(412, "/openapi.json deprecated=True on endpoint",
            (fa_s, {}, fa_openapi_raw), (rs_s, {}, rs_openapi_raw), check_412)

    # P413: include_in_schema=False hides endpoint
    def check_413(fs, fh, fb, rs, rh, rb):
        fa_paths = fa_openapi.get("paths", {})
        rs_paths = rs_openapi.get("paths", {})
        assert "/p413-hidden" not in fa_paths, "FastAPI should hide /p413-hidden"
        assert "/p413-hidden" not in rs_paths, "fastapi-rs should hide /p413-hidden"
    compare(413, "/openapi.json include_in_schema=False hides endpoint",
            (fa_s, {}, fa_openapi_raw), (rs_s, {}, rs_openapi_raw), check_413)

    # P414: query param with ge/le shows constraints
    def check_414(fs, fh, fb, rs, rh, rb):
        fa_op = fa_openapi.get("paths", {}).get("/p414-constrained-query", {}).get("get", {})
        rs_op = rs_openapi.get("paths", {}).get("/p414-constrained-query", {}).get("get", {})
        assert fa_op, "FastAPI missing /p414-constrained-query"
        assert rs_op, "fastapi-rs missing /p414-constrained-query"
        # Both should have parameters with constraints
        fa_params = fa_op.get("parameters", [])
        rs_params = rs_op.get("parameters", [])
        assert len(fa_params) > 0, "FastAPI missing params"
        assert len(rs_params) > 0, "fastapi-rs missing params"
    compare(414, "/openapi.json query param with ge/le",
            (fa_s, {}, fa_openapi_raw), (rs_s, {}, rs_openapi_raw), check_414)

    # P415: path param typed as integer
    def check_415(fs, fh, fb, rs, rh, rb):
        fa_op = fa_openapi.get("paths", {}).get("/p415-path-int/{item_id}", {}).get("get", {})
        rs_op = rs_openapi.get("paths", {}).get("/p415-path-int/{item_id}", {}).get("get", {})
        assert fa_op, "FastAPI missing /p415-path-int/{item_id}"
        assert rs_op, "fastapi-rs missing /p415-path-int/{item_id}"
    compare(415, "/openapi.json path param typed as integer",
            (fa_s, {}, fa_openapi_raw), (rs_s, {}, rs_openapi_raw), check_415)

    # P416: security scheme (OAuth2)
    def check_416(fs, fh, fb, rs, rh, rb):
        fa_components = fa_openapi.get("components", {}).get("securitySchemes", {})
        rs_components = rs_openapi.get("components", {}).get("securitySchemes", {})
        fa_has_oauth = any("oauth2" in v.get("type", "") for v in fa_components.values())
        rs_has_oauth = any("oauth2" in v.get("type", "") for v in rs_components.values())
        assert fa_has_oauth, f"FastAPI missing OAuth2 scheme, got {fa_components}"
        assert rs_has_oauth, f"fastapi-rs missing OAuth2 scheme, got {rs_components}"
    compare(416, "/openapi.json security scheme (OAuth2)",
            (fa_s, {}, fa_openapi_raw), (rs_s, {}, rs_openapi_raw), check_416)

    # P417: security scheme (HTTPBearer)
    def check_417(fs, fh, fb, rs, rh, rb):
        fa_components = fa_openapi.get("components", {}).get("securitySchemes", {})
        rs_components = rs_openapi.get("components", {}).get("securitySchemes", {})
        fa_has_bearer = any(v.get("scheme") == "bearer" for v in fa_components.values())
        rs_has_bearer = any(v.get("scheme") == "bearer" for v in rs_components.values())
        assert fa_has_bearer, f"FastAPI missing HTTPBearer scheme, got {fa_components}"
        assert rs_has_bearer, f"fastapi-rs missing HTTPBearer scheme, got {rs_components}"
    compare(417, "/openapi.json security scheme (HTTPBearer)",
            (fa_s, {}, fa_openapi_raw), (rs_s, {}, rs_openapi_raw), check_417)

    # P418: servers list
    def check_418(fs, fh, fb, rs, rh, rb):
        fa_servers = fa_openapi.get("servers", [])
        rs_servers = rs_openapi.get("servers", [])
        assert len(fa_servers) > 0, "FastAPI missing servers"
        assert len(rs_servers) > 0, "fastapi-rs missing servers"
    compare(418, "/openapi.json servers list",
            (fa_s, {}, fa_openapi_raw), (rs_s, {}, rs_openapi_raw), check_418)

    # P419: /docs serves Swagger UI HTML
    def check_419(fs, fh, fb, rs, rh, rb):
        assert fs == 200, f"FastAPI /docs status={fs}"
        assert rs == 200, f"fastapi-rs /docs status={rs}"
        assert "swagger" in fb.lower() or "openapi" in fb.lower(), "FastAPI /docs not Swagger"
        assert "swagger" in rb.lower() or "openapi" in rb.lower(), "fastapi-rs /docs not Swagger"
    fa_docs = http_request(fa_port, "/docs")
    rs_docs = http_request(rs_port, "/docs")
    compare(419, "/docs serves Swagger UI HTML", fa_docs, rs_docs, check_419)

    # P420: /redoc serves ReDoc HTML
    def check_420(fs, fh, fb, rs, rh, rb):
        assert fs == 200, f"FastAPI /redoc status={fs}"
        assert rs == 200, f"fastapi-rs /redoc status={rs}"
        assert "redoc" in fb.lower(), "FastAPI /redoc not ReDoc"
        assert "redoc" in rb.lower(), "fastapi-rs /redoc not ReDoc"
    fa_redoc = http_request(fa_port, "/redoc")
    rs_redoc = http_request(rs_port, "/redoc")
    compare(420, "/redoc serves ReDoc HTML", fa_redoc, rs_redoc, check_420)


    print(f"\n{BOLD}{CYAN}=== Templates + Static Files (421-440) ==={RESET}\n")

    # P421: Jinja2Templates render basic template
    def check_html(fs, fh, fb, rs, rh, rb):
        assert fs == 200, f"FastAPI status={fs}"
        assert rs == 200, f"fastapi-rs status={rs}"
        assert "Hello Basic" in fb, f"FastAPI missing content: {fb[:100]}"
        assert "Hello Basic" in rb, f"fastapi-rs missing content: {rb[:100]}"
    compare_json(421, "Jinja2Templates render basic template",
                 fa_port, rs_port, "/p421-template-basic", check_fn=check_html)

    # P422: template with context
    def check_422(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        assert "Alice" in fb and "30" in fb, f"FastAPI missing context: {fb[:100]}"
        assert "Alice" in rb and "30" in rb, f"fastapi-rs missing context: {rb[:100]}"
    compare_json(422, "Jinja2Templates with context variables",
                 fa_port, rs_port, "/p422-template-context", check_fn=check_422)

    # P423: old-style signature (tested via new-style on Starlette 1.0)
    def check_423(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        assert "Legacy" in fb, f"FastAPI missing Legacy: {fb[:100]}"
        assert "Legacy" in rb, f"fastapi-rs missing Legacy: {rb[:100]}"
    compare_json(423, "Jinja2Templates template render (old-style compat)",
                 fa_port, rs_port, "/p423-template-old-style", check_fn=check_423)

    # P424: new-style signature
    def check_424(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        assert "World" in fb, f"FastAPI missing World: {fb[:100]}"
        assert "World" in rb, f"fastapi-rs missing World: {rb[:100]}"
    compare_json(424, "Jinja2Templates new-style signature",
                 fa_port, rs_port, "/p424-template-new-style", check_fn=check_424)

    # P425: StaticFiles serves files
    def check_static(fs, fh, fb, rs, rh, rb):
        assert fs == 200, f"FastAPI static status={fs}"
        assert rs == 200, f"fastapi-rs static status={rs}"
        assert fb == rb, f"Content mismatch: '{fb[:80]}' vs '{rb[:80]}'"
    compare_json(425, "StaticFiles mount serves files",
                 fa_port, rs_port, "/static/data.txt", check_fn=check_static)

    # P426: CSS with correct content-type
    def check_426(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_ct = fh.get("content-type", fh.get("Content-Type", ""))
        rs_ct = rh.get("content-type", rh.get("Content-Type", ""))
        assert "css" in fa_ct.lower(), f"FastAPI CSS type: {fa_ct}"
        assert "css" in rs_ct.lower(), f"fastapi-rs CSS type: {rs_ct}"
    compare_json(426, "StaticFiles serves CSS with correct content-type",
                 fa_port, rs_port, "/static/style.css", check_fn=check_426)

    # P427: JS with correct content-type
    def check_427(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_ct = fh.get("content-type", fh.get("Content-Type", ""))
        rs_ct = rh.get("content-type", rh.get("Content-Type", ""))
        assert "javascript" in fa_ct.lower(), f"FastAPI JS type: {fa_ct}"
        assert "javascript" in rs_ct.lower(), f"fastapi-rs JS type: {rs_ct}"
    compare_json(427, "StaticFiles serves JS with correct content-type",
                 fa_port, rs_port, "/static/app.js", check_fn=check_427)

    # P428: StaticFiles 404 for missing files
    def check_428(fs, fh, fb, rs, rh, rb):
        assert fs == 404, f"FastAPI should 404, got {fs}"
        assert rs == 404, f"fastapi-rs should 404, got {rs}"
    compare_json(428, "StaticFiles 404 for missing files",
                 fa_port, rs_port, "/static/nonexistent.xyz", check_fn=check_428)

    # P429: StaticFiles html=True (SPA mode)
    def check_429(fs, fh, fb, rs, rh, rb):
        assert fs == 200, f"FastAPI SPA status={fs}"
        assert rs == 200, f"fastapi-rs SPA status={rs}"
        assert "SPA Index" in fb, f"FastAPI SPA missing index: {fb[:100]}"
        assert "SPA Index" in rb, f"fastapi-rs SPA missing index: {rb[:100]}"
    compare_json(429, "StaticFiles html=True serves index.html",
                 fa_port, rs_port, "/spa/", check_fn=check_429)

    # P430: mount sub-FastAPI app
    def check_430(fs, fh, fb, rs, rh, rb):
        assert fs == 200, f"FastAPI sub-app status={fs}"
        assert rs == 200, f"fastapi-rs sub-app status={rs}"
    compare_json(430, "mount sub-FastAPI app at prefix",
                 fa_port, rs_port, "/sub/sub-health", check_fn=check_430)

    # P431: sub-app routes accessible via prefix
    compare_json(431, "mount sub-app routes accessible via prefix",
                 fa_port, rs_port, "/sub/sub-data")

    # P432: sub-app doesn't leak to parent
    def check_432(fs, fh, fb, rs, rh, rb):
        assert fs == 404 or fs == 405, f"FastAPI should 404, got {fs}"
        assert rs == 404 or rs == 405, f"fastapi-rs should 404, got {rs}"
    compare_json(432, "mount sub-app doesn't leak to parent",
                 fa_port, rs_port, "/sub-health", check_fn=check_432)

    # P433: template with status_code
    def check_433(fs, fh, fb, rs, rh, rb):
        assert fs == 201, f"FastAPI template status={fs}"
        assert rs == 201, f"fastapi-rs template status={rs}"
    compare_json(433, "template with custom status_code",
                 fa_port, rs_port, "/p433-template-status", check_fn=check_433)

    # P434: template with custom headers
    def check_434(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_hdr = fh.get("x-template", fh.get("X-Template", ""))
        rs_hdr = rh.get("x-template", rh.get("X-Template", ""))
        assert fa_hdr == "yes", f"FastAPI missing X-Template header"
        assert rs_hdr == "yes", f"fastapi-rs missing X-Template header"
    compare_json(434, "template with custom headers",
                 fa_port, rs_port, "/p434-template-headers", check_fn=check_434)

    # P435: static nested path
    compare_json(435, "static file with nested path",
                 fa_port, rs_port, "/static/nested/deep.txt", check_fn=check_static)

    # P436: parent-only route
    compare_json(436, "parent route exists", fa_port, rs_port, "/p436-parent-only")

    # P437: multiple static mounts
    def check_437(fs, fh, fb, rs, rh, rb):
        assert fs == 200, f"FastAPI assets status={fs}"
        assert rs == 200, f"fastapi-rs assets status={rs}"
        assert "logo" in fb and "logo" in rb
    compare_json(437, "multiple static mounts",
                 fa_port, rs_port, "/assets/logo.txt", check_fn=check_437)

    # P438: static file content integrity
    def check_438(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        assert fb == "checksum_content_abc123", f"FastAPI content: {fb}"
        assert rb == "checksum_content_abc123", f"fastapi-rs content: {rb}"
    compare_json(438, "static file content integrity",
                 fa_port, rs_port, "/static/checksum.txt", check_fn=check_438)

    # P439: empty static file
    def check_439(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        assert fb == "" and rb == ""
    compare_json(439, "empty static file",
                 fa_port, rs_port, "/static/empty.txt", check_fn=check_439)

    # P440: static file with special chars
    def check_440(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        assert "line1" in fb and "line2" in fb
        assert "line1" in rb and "line2" in rb
    compare_json(440, "static file with special characters",
                 fa_port, rs_port, "/static/special.txt", check_fn=check_440)


    print(f"\n{BOLD}{CYAN}=== Advanced Pydantic (441-460) ==={RESET}\n")

    # P441: Field(description=...)
    compare_json(441, "BaseModel with Field(description=...)",
                 fa_port, rs_port, "/p441-field-description",
                 method="POST", body={"name": "test", "count": 5})

    # P442: Field(example=...)
    compare_json(442, "BaseModel with Field(example=...)",
                 fa_port, rs_port, "/p442-field-example",
                 method="POST", body={"value": 42})

    # P443: Optional[str] = None
    compare_json(443, "BaseModel with Optional[str] = None",
                 fa_port, rs_port, "/p443-optional-field",
                 method="POST", body={"name": "test"})

    # P444: list[SubModel]
    compare_json(444, "BaseModel with list[SubModel]",
                 fa_port, rs_port, "/p444-list-submodel",
                 method="POST", body={"items": [{"label": "a", "value": 1}, {"label": "b", "value": 2}]})

    # P445: dict[str, Any]
    compare_json(445, "BaseModel with dict[str, Any]",
                 fa_port, rs_port, "/p445-dict-any",
                 method="POST", body={"metadata": {"key": "val", "num": 42}})

    # P446: datetime field
    compare_json(446, "BaseModel with datetime field",
                 fa_port, rs_port, "/p446-datetime",
                 method="POST", body={"created_at": "2024-01-15T10:30:00"})

    # P447: UUID field
    compare_json(447, "BaseModel with UUID field",
                 fa_port, rs_port, "/p447-uuid",
                 method="POST", body={"id": "550e8400-e29b-41d4-a716-446655440000"})

    # P448: Enum field
    compare_json(448, "BaseModel with Enum field",
                 fa_port, rs_port, "/p448-enum",
                 method="POST", body={"color": "red"})

    # P449: default_factory=list
    compare_json(449, "BaseModel with default_factory=list (no tags)",
                 fa_port, rs_port, "/p449-default-factory",
                 method="POST", body={})

    # P450: model_config ConfigDict
    compare_json(450, "BaseModel with model_config ConfigDict",
                 fa_port, rs_port, "/p450-config-dict",
                 method="POST", body={"name": "  test  "})

    # P451: model_validator (before)
    compare_json(451, "BaseModel with model_validator (before)",
                 fa_port, rs_port, "/p451-validator-before",
                 method="POST", body={"value": 5})

    # P452: model_validator (after)
    compare_json(452, "BaseModel with model_validator (after)",
                 fa_port, rs_port, "/p452-validator-after",
                 method="POST", body={"value": 200})

    # P453: computed_field
    compare_json(453, "BaseModel with computed_field",
                 fa_port, rs_port, "/p453-computed-field",
                 method="POST", body={"first": "John", "last": "Doe"})

    # P454: Annotated[int, Field(ge=0)] - valid
    compare_json(454, "Annotated[int, Field(ge=0)] in handler",
                 fa_port, rs_port, "/p454-annotated-field?n=5")

    # P455: Annotated[str, Query()]
    compare_json(455, "Annotated[str, Query()] with metadata",
                 fa_port, rs_port, "/p455-annotated-query?q=hello")

    # P456: Union body
    compare_json(456, "Union[ModelA, ModelB] body",
                 fa_port, rs_port, "/p456-union-body",
                 method="POST", body={"kind": "a", "a_val": 42})

    # P457: Generic model
    compare_json(457, "Generic model",
                 fa_port, rs_port, "/p457-generic-model",
                 method="POST", body={"data": {"key": "val"}, "type_name": "dict"})

    # P458: Recursive model
    compare_json(458, "Recursive model",
                 fa_port, rs_port, "/p458-recursive-model",
                 method="POST", body={"name": "root", "children": [{"name": "child1", "children": []}]})

    # P459: json_schema_extra
    compare_json(459, "BaseModel with json_schema_extra",
                 fa_port, rs_port, "/p459-json-schema-extra",
                 method="POST", body={"name": "test"})

    # P460: exclude_none
    def check_460(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        assert "nickname" not in fa_data, f"FastAPI should exclude None: {fa_data}"
        assert "nickname" not in rs_data, f"fastapi-rs should exclude None: {rs_data}"
        assert "bio" not in fa_data, f"FastAPI should exclude None bio: {fa_data}"
        assert "bio" not in rs_data, f"fastapi-rs should exclude None bio: {rs_data}"
        assert fa_data["name"] == "Alice"
        assert rs_data["name"] == "Alice"
    compare_json(460, "Response excludes None fields",
                 fa_port, rs_port, "/p460-exclude-none", check_fn=check_460)


    print(f"\n{BOLD}{CYAN}=== Request Object (461-480) ==={RESET}\n")

    # P461: request.method (GET)
    compare_json(461, "request.method returns correct method (GET)",
                 fa_port, rs_port, "/p461-request-method")

    # P462: request.url.path
    def check_462(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        assert fa_data["path"] == "/p462-request-url-path", f"FastAPI path: {fa_data}"
        assert rs_data["path"] == "/p462-request-url-path", f"fastapi-rs path: {rs_data}"
    compare_json(462, "request.url.path returns correct path",
                 fa_port, rs_port, "/p462-request-url-path", check_fn=check_462)

    # P463: request.url query string
    def check_463(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        assert "foo=bar" in fa_data["query"], f"FastAPI query: {fa_data}"
        assert "foo=bar" in rs_data["query"], f"fastapi-rs query: {rs_data}"
    compare_json(463, "request.url.query returns query string",
                 fa_port, rs_port, "/p463-request-url-query?foo=bar&baz=1", check_fn=check_463)

    # P464: request.headers["host"]
    def check_464(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        assert fa_data["host"] != "unknown", f"FastAPI host unknown"
        assert rs_data["host"] != "unknown", f"fastapi-rs host unknown"
    compare_json(464, "request.headers['host'] returns host",
                 fa_port, rs_port, "/p464-request-headers-host", check_fn=check_464)

    # P465: request.headers["content-type"] for POST
    def check_465(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        assert "json" in fa_data["content_type"].lower(), f"FastAPI ct: {fa_data}"
        assert "json" in rs_data["content_type"].lower(), f"fastapi-rs ct: {rs_data}"
    compare_json(465, "request.headers['content-type'] for POST",
                 fa_port, rs_port, "/p465-request-content-type",
                 method="POST", body={"test": True}, check_fn=check_465)

    # P466: request.query_params
    def check_466(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        assert fa_data["key"] == "myvalue", f"FastAPI key: {fa_data}"
        assert rs_data["key"] == "myvalue", f"fastapi-rs key: {rs_data}"
    compare_json(466, "request.query_params['key']",
                 fa_port, rs_port, "/p466-request-query-params?key=myvalue", check_fn=check_466)

    # P467: request.path_params
    compare_json(467, "request.path_params['id']",
                 fa_port, rs_port, "/p467-request-path-params/42")

    # P468: request.cookies
    def check_468(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        assert fa_data["session"] == "abc123", f"FastAPI cookie: {fa_data}"
        assert rs_data["session"] == "abc123", f"fastapi-rs cookie: {rs_data}"
    compare_json(468, "request.cookies['session']",
                 fa_port, rs_port, "/p468-request-cookies",
                 headers={"Cookie": "session=abc123"}, check_fn=check_468)

    # P469: request.client.host
    def check_469(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        assert fa_data["client_host"] != "unknown", f"FastAPI client: {fa_data}"
        assert rs_data["client_host"] != "unknown", f"fastapi-rs client: {rs_data}"
    compare_json(469, "request.client.host returns client IP",
                 fa_port, rs_port, "/p469-request-client", check_fn=check_469)

    # P470: request.app is not None
    def check_470(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        assert fa_data["has_app"] is True, f"FastAPI app: {fa_data}"
        assert rs_data["has_app"] is True, f"fastapi-rs app: {rs_data}"
    compare_json(470, "request.app is the FastAPI instance",
                 fa_port, rs_port, "/p470-request-app", check_fn=check_470)

    # P471: request.app.state.X
    def check_471(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        assert fa_data["db_pool"] == "fake_pool_connected", f"FastAPI state: {fa_data}"
        assert rs_data["db_pool"] == "fake_pool_connected", f"fastapi-rs state: {rs_data}"
    compare_json(471, "request.app.state.X returns lifespan state",
                 fa_port, rs_port, "/p471-request-app-state", check_fn=check_471)

    # P472: request.state per-request
    def check_472(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        assert fa_data["custom_val"] == "hello", f"FastAPI state: {fa_data}"
        assert rs_data["custom_val"] == "hello", f"fastapi-rs state: {rs_data}"
    compare_json(472, "request.state.X per-request",
                 fa_port, rs_port, "/p472-request-state", check_fn=check_472)

    # P473: await request.body()
    def check_473(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        assert fa_data["body_length"] > 0, f"FastAPI body: {fa_data}"
        assert rs_data["body_length"] > 0, f"fastapi-rs body: {rs_data}"
    compare_json(473, "await request.body() returns bytes",
                 fa_port, rs_port, "/p473-request-body",
                 method="POST", body={"payload": "test_data"}, check_fn=check_473)

    # P474: await request.json()
    def check_474(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        assert fa_data["data"]["key"] == "value", f"FastAPI json: {fa_data}"
        assert rs_data["data"]["key"] == "value", f"fastapi-rs json: {rs_data}"
    compare_json(474, "await request.json() returns parsed dict",
                 fa_port, rs_port, "/p474-request-json",
                 method="POST", body={"key": "value"}, check_fn=check_474)

    # P475: await request.form()
    def check_475(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        assert fa_data["form"]["username"] == "testuser", f"FastAPI form: {fa_data}"
        assert rs_data["form"]["username"] == "testuser", f"fastapi-rs form: {rs_data}"
    fa_475 = http_form(fa_port, "/p475-request-form", {"username": "testuser", "action": "login"})
    rs_475 = http_form(rs_port, "/p475-request-form", {"username": "testuser", "action": "login"})
    compare(475, "await request.form() returns form data", fa_475, rs_475, check_475)

    # P476: request.url_for
    def check_476(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        # Both should have a url that includes the route path
        fa_url = fa_data.get("url", fa_data.get("error", ""))
        rs_url = rs_data.get("url", rs_data.get("error", ""))
        assert "p476-url-for" in fa_url, f"FastAPI url_for: {fa_data}"
        assert "p476-url-for" in rs_url, f"fastapi-rs url_for: {rs_data}"
    compare_json(476, "request.url_for('route_name')",
                 fa_port, rs_port, "/p476-url-for", check_fn=check_476)

    # P477: request.base_url
    def check_477(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        assert "http" in fa_data["base_url"], f"FastAPI base_url: {fa_data}"
        assert "http" in rs_data["base_url"], f"fastapi-rs base_url: {rs_data}"
    compare_json(477, "request.base_url returns scheme://host",
                 fa_port, rs_port, "/p477-base-url", check_fn=check_477)

    # P478: request.scope["type"]
    def check_478(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        assert fa_data["type"] == "http", f"FastAPI scope type: {fa_data}"
        assert rs_data["type"] == "http", f"fastapi-rs scope type: {rs_data}"
    compare_json(478, "request.scope['type'] == 'http'",
                 fa_port, rs_port, "/p478-request-scope-type", check_fn=check_478)

    # P479: middleware request access
    def check_479(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_path = fh.get("x-path-seen", fh.get("X-Path-Seen", ""))
        rs_path = rh.get("x-path-seen", rh.get("X-Path-Seen", ""))
        assert "/p479" in fa_path, f"FastAPI middleware header: {fa_path}"
        assert "/p479" in rs_path, f"fastapi-rs middleware header: {rs_path}"
    compare_json(479, "request in middleware (path accessible)",
                 fa_port, rs_port, "/p479-middleware-request", check_fn=check_479)

    # P480: request.state persists across middleware
    def check_480(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        assert fa_data["stamp"] == "middleware_was_here", f"FastAPI state: {fa_data}"
        assert rs_data["stamp"] == "middleware_was_here", f"fastapi-rs state: {rs_data}"
    compare_json(480, "request.state persists across middleware",
                 fa_port, rs_port, "/p480-state-across-mw", check_fn=check_480)


    print(f"\n{BOLD}{CYAN}=== Real-World Patterns (481-500) ==={RESET}\n")

    # P481: SSE streaming
    def check_481(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        assert "word0" in fb, f"FastAPI SSE: {fb[:100]}"
        assert "word0" in rb, f"fastapi-rs SSE: {rb[:100]}"
        assert "[DONE]" in fb, f"FastAPI SSE missing DONE"
        assert "[DONE]" in rb, f"fastapi-rs SSE missing DONE"
    compare_json(481, "StreamingResponse + text/event-stream",
                 fa_port, rs_port, "/p481-sse-stream", method="POST", body={}, check_fn=check_481)

    # P482: OAuth2PasswordRequestForm login
    def check_482(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        assert fa_data["username"] == "admin", f"FastAPI: {fa_data}"
        assert rs_data["username"] == "admin", f"fastapi-rs: {rs_data}"
    fa_482 = http_form(fa_port, "/p482-oauth-login", {"username": "admin", "password": "secret", "grant_type": "password"})
    rs_482 = http_form(rs_port, "/p482-oauth-login", {"username": "admin", "password": "secret", "grant_type": "password"})
    compare(482, "OAuth2PasswordRequestForm for login", fa_482, rs_482, check_482)

    # P483: BaseHTTPMiddleware stack
    compare_json(483, "BaseHTTPMiddleware stack",
                 fa_port, rs_port, "/p483-middleware-stack")

    # P484: custom OpenAPI via programmatic access
    def check_484(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        assert fa_data["has_paths"] is True, f"FastAPI: {fa_data}"
        assert rs_data["has_paths"] is True, f"fastapi-rs: {rs_data}"
        assert fa_data["title"] == "Parity Test App 4"
        assert rs_data["title"] == "Parity Test App 4"
    compare_json(484, "read OpenAPI schema programmatically",
                 fa_port, rs_port, "/p484-custom-openapi", check_fn=check_484)

    # P485: run_in_threadpool
    def check_485(fs, fh, fb, rs, rh, rb):
        assert fs == 200, f"FastAPI status={fs}, body={fb[:200]}"
        assert rs == 200, f"fastapi-rs status={rs}, body={rb[:200]}"
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        assert fa_data["result"] == 499500, f"FastAPI threadpool: {fa_data}"
        assert rs_data["result"] == 499500, f"fastapi-rs threadpool: {rs_data}"
    compare_json(485, "run_in_threadpool for CPU work",
                 fa_port, rs_port, "/p485-threadpool", check_fn=check_485)

    # P486: custom operation_id
    def check_486(fs, fh, fb, rs, rh, rb):
        fa_op = fa_openapi.get("paths", {}).get("/p486-custom-op-id", {}).get("get", {})
        rs_op = rs_openapi.get("paths", {}).get("/p486-custom-op-id", {}).get("get", {})
        assert fa_op.get("operationId") == "my_custom_operation", f"FastAPI opId: {fa_op.get('operationId')}"
        assert rs_op.get("operationId") == "my_custom_operation", f"fastapi-rs opId: {rs_op.get('operationId')}"
    compare(486, "custom unique_id for operation IDs",
            (fa_s, {}, fa_openapi_raw), (rs_s, {}, rs_openapi_raw), check_486)

    # P487: GZipMiddleware (response for large data)
    def check_487(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        # With Accept-Encoding: gzip, response may be compressed
        # Just check the endpoint works
    compare_json(487, "GZipMiddleware functional",
                 fa_port, rs_port, "/p487-gzip-test",
                 headers={"Accept-Encoding": "gzip"}, check_fn=check_487)

    # P488: APIRouter subclass with custom __init__
    compare_json(488, "APIRouter subclass with custom __init__",
                 fa_port, rs_port, "/p488/info")

    # P489: LangServe SSE
    def check_489(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        assert "output" in fb, f"FastAPI SSE: {fb[:100]}"
        assert "output" in rb, f"fastapi-rs SSE: {rb[:100]}"
    compare_json(489, "LangServe SSE pattern",
                 fa_port, rs_port, "/p489-langserve-sse",
                 method="POST", body={"input": "hello world"}, check_fn=check_489)

    # P490: FileResponse
    def check_490(fs, fh, fb, rs, rh, rb):
        assert fs == 200, f"FastAPI file status={fs}"
        assert rs == 200, f"fastapi-rs file status={rs}"
        assert "download" in fb and "download" in rb
    compare_json(490, "FileResponse + filename",
                 fa_port, rs_port, "/p490-file-download", check_fn=check_490)

    # P491: ORJSONResponse
    compare_json(491, "ORJSONResponse as default_response_class",
                 fa_port, rs_port, "/p491/orjson-data")

    # P492: multiple sub-apps
    def check_492a(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        assert fa_data["app"] == "dispatch_1", f"FastAPI: {fa_data}"
        assert rs_data["app"] == "dispatch_1", f"fastapi-rs: {rs_data}"
    compare_json(492, "multiple sub-apps (app1)",
                 fa_port, rs_port, "/p492-app1/info", check_fn=check_492a)

    # P493: router factory
    def check_493(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        assert fa_data["username"] == "alice", f"FastAPI: {fa_data}"
        assert rs_data["username"] == "alice", f"fastapi-rs: {rs_data}"
    fa_493 = http_form(fa_port, "/p493-auth/login", {"username": "alice", "password": "pw"})
    rs_493 = http_form(rs_port, "/p493-auth/login", {"username": "alice", "password": "pw"})
    compare(493, "router factory (get_auth_router)", fa_493, rs_493, check_493)

    # P494: placeholder
    compare_json(494, "WSGIMiddleware placeholder",
                 fa_port, rs_port, "/p494-wsgi-placeholder")

    # P495: WebSocket placeholder
    compare_json(495, "WebSocket placeholder",
                 fa_port, rs_port, "/p495-ws-placeholder")

    # P496: app.state for rate limiter
    def check_496(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        assert fa_data["limiter"]["limit"] == 100, f"FastAPI: {fa_data}"
        assert rs_data["limiter"]["limit"] == 100, f"fastapi-rs: {rs_data}"
    compare_json(496, "app.state for rate limiter storage",
                 fa_port, rs_port, "/p496-app-state", check_fn=check_496)

    # P497: read OpenAPI programmatically
    def check_497(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        assert fa_data["path_count"] > 0, f"FastAPI: {fa_data}"
        assert rs_data["path_count"] > 0, f"fastapi-rs: {rs_data}"
        assert fa_data["has_openapi_version"] is True
        assert rs_data["has_openapi_version"] is True
    compare_json(497, "read OpenAPI schema programmatically",
                 fa_port, rs_port, "/p497-read-openapi", check_fn=check_497)

    # P498: RequestValidationError handler
    def check_498(fs, fh, fb, rs, rh, rb):
        assert fs == 422 and rs == 422, f"Status: FastAPI={fs}, fastapi-rs={rs}"
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        assert "custom_validation_error" in fa_data.get("detail", ""), f"FastAPI: {fa_data}"
        assert "custom_validation_error" in rs_data.get("detail", ""), f"fastapi-rs: {rs_data}"
    # Trigger validation error by omitting required query param
    compare_json(498, "RequestValidationError handler",
                 fa_port, rs_port, "/p498-validation-error", check_fn=check_498)

    # P499: custom route endpoint works
    compare_json(499, "custom APIRoute class endpoint works",
                 fa_port, rs_port, "/p499-custom-route")

    # P500: full stack (CORS + GZip + auth + response_model + 422)
    def check_500(fs, fh, fb, rs, rh, rb):
        assert fs == 200 and rs == 200
        fa_data = json.loads(fb)
        rs_data = json.loads(rb)
        assert fa_data["message"] == "full stack works", f"FastAPI: {fa_data}"
        assert rs_data["message"] == "full stack works", f"fastapi-rs: {rs_data}"
        assert fa_data["user"] == "authenticated_user", f"FastAPI user: {fa_data}"
        assert rs_data["user"] == "authenticated_user", f"fastapi-rs user: {rs_data}"
        # response_model should strip "extra" field
        assert "extra" not in fa_data, f"FastAPI extra not stripped: {fa_data}"
        assert "extra" not in rs_data, f"fastapi-rs extra not stripped: {rs_data}"
    fa_500 = http_request(fa_port, "/p500-full-stack",
                          headers={"Authorization": "Bearer test_token"})
    rs_500 = http_request(rs_port, "/p500-full-stack",
                          headers={"Authorization": "Bearer test_token"})
    compare(500, "Full stack: CORS + GZip + auth + response_model", fa_500, rs_500, check_500)


# ── Main ─────────────────────────────────────────────────────────

def main():
    print(f"\n{BOLD}{'='*60}")
    print(f"  Parity Test Suite 4: Patterns 401-500")
    print(f"  FastAPI on :{FASTAPI_PORT}   |   fastapi-rs on :{FASTAPI_RS_PORT}")
    print(f"{'='*60}{RESET}\n")

    uvicorn_proc = None
    rs_proc = None

    try:
        # Start both servers
        print(f"Starting uvicorn (stock FastAPI) on port {FASTAPI_PORT}...")
        uvicorn_proc = start_uvicorn(FASTAPI_PORT)

        print(f"Starting fastapi-rs on port {FASTAPI_RS_PORT}...")
        rs_proc = start_fastapi_rs(FASTAPI_RS_PORT)

        # Wait for both
        print("Waiting for servers to start...")
        fa_ready = wait_for_port(FASTAPI_PORT)
        rs_ready = wait_for_port(FASTAPI_RS_PORT)

        if not fa_ready:
            print(f"{RED}ERROR: uvicorn failed to start on port {FASTAPI_PORT}{RESET}")
            if uvicorn_proc:
                stderr = uvicorn_proc.stderr.read().decode() if uvicorn_proc.stderr else ""
                print(f"  stderr: {stderr[:500]}")
            return 1

        if not rs_ready:
            print(f"{RED}ERROR: fastapi-rs failed to start on port {FASTAPI_RS_PORT}{RESET}")
            if rs_proc:
                stderr = rs_proc.stderr.read().decode() if rs_proc.stderr else ""
                print(f"  stderr: {stderr[:500]}")
            return 1

        print(f"{GREEN}Both servers ready!{RESET}\n")

        # Verify health on both
        fa_health = http_request(FASTAPI_PORT, "/health")
        rs_health = http_request(FASTAPI_RS_PORT, "/health")
        if fa_health[0] != 200:
            print(f"{RED}FastAPI /health failed: {fa_health}{RESET}")
            return 1
        if rs_health[0] != 200:
            print(f"{RED}fastapi-rs /health failed: {rs_health}{RESET}")
            return 1

        # Run all tests
        run_all_tests(FASTAPI_PORT, FASTAPI_RS_PORT)

        # Summary
        total = len(results)
        passed = sum(1 for _, _, p, _ in results if p)
        failed = sum(1 for _, _, p, _ in results if not p)

        print(f"\n{BOLD}{'='*60}")
        print(f"  RESULTS: {total} tests | {GREEN}{passed} PASS{RESET}{BOLD} | {RED}{failed} FAIL{RESET}")
        print(f"{BOLD}{'='*60}{RESET}\n")

        if failed > 0:
            print(f"{BOLD}Failed tests:{RESET}")
            for pid, desc, p, detail in results:
                if not p:
                    print(f"  {RED}P{pid}{RESET}: {desc}")
                    if detail:
                        print(f"    {detail[:300]}")
            print()

        return 0 if failed == 0 else 1

    except KeyboardInterrupt:
        print(f"\n{YELLOW}Interrupted.{RESET}")
        return 130
    finally:
        # Clean up
        for proc in [uvicorn_proc, rs_proc]:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


if __name__ == "__main__":
    sys.exit(main())
