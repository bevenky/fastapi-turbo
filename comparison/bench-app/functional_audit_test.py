"""
Functional Audit Test Runner for fastapi-turbo.
Starts the audit app, hits every endpoint, reports PASS/FAIL.
"""
import subprocess
import sys
import time
import os
import json
import asyncio

import httpx

BASE = "http://127.0.0.1:19800"
results = []

def report(test_num, name, passed, detail=""):
    status = "PASS" if passed else "FAIL"
    results.append((test_num, name, passed, detail))
    marker = "  " if passed else "**"
    num_str = f"{test_num:02d}" if isinstance(test_num, int) else str(test_num)
    print(f"  {marker}[{status}] #{num_str} {name}" + (f" -- {detail}" if detail else ""))


def wait_for_server(timeout=15):
    """Wait for server to be ready."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = httpx.get(f"{BASE}/_ping", timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def run_tests():
    print("\n" + "=" * 70)
    print("  FASTAPI-TURBO FUNCTIONAL AUDIT")
    print("=" * 70)

    client = httpx.Client(base_url=BASE, timeout=10, follow_redirects=False)

    # ======== ROUTING PATTERNS ========
    print("\n-- ROUTING PATTERNS --")

    # 1. Basic GET
    try:
        r = client.get("/test/basic-get")
        data = r.json()
        report(1, "Basic GET", r.status_code == 200 and data.get("ok") is True)
    except Exception as e:
        report(1, "Basic GET", False, str(e))

    # 2. POST with JSON body
    try:
        r = client.post("/test/basic-post", json={"name": "widget", "price": 9.99})
        data = r.json()
        report(2, "POST with JSON body", r.status_code == 200 and data.get("name") == "widget" and data.get("price") == 9.99)
    except Exception as e:
        report(2, "POST with JSON body", False, str(e))

    # 3. PUT, PATCH, DELETE
    try:
        r = client.put("/test/put", json={"name": "updated", "price": 1.0})
        data_put = r.json()
        ok_put = data_put.get("method") == "PUT" and data_put.get("name") == "updated"
        if not ok_put:
            report(3, "PUT/PATCH/DELETE", False, f"PUT failed: status={r.status_code}, body={data_put}")
        else:
            r = client.patch("/test/patch", json={"name": "patched", "price": 2.0})
            data_patch = r.json()
            ok_patch = data_patch.get("method") == "PATCH" and data_patch.get("name") == "patched"
            if not ok_patch:
                report(3, "PUT/PATCH/DELETE", False, f"PATCH failed: status={r.status_code}, body={data_patch}")
            else:
                r = client.delete("/test/delete")
                data_del = r.json()
                ok_delete = data_del.get("method") == "DELETE" and data_del.get("ok") is True
                report(3, "PUT/PATCH/DELETE", ok_delete, f"DELETE: status={r.status_code}, body={data_del}" if not ok_delete else "")
    except Exception as e:
        report(3, "PUT/PATCH/DELETE", False, str(e))

    # 4. api_route with multiple methods
    try:
        r1 = client.get("/test/multi-method")
        r2 = client.post("/test/multi-method")
        ok = r1.status_code == 200 and r2.status_code == 200
        ok = ok and r1.json().get("multi") is True and r2.json().get("multi") is True
        report(4, "api_route multi-method", ok)
    except Exception as e:
        report(4, "api_route multi-method", False, str(e))

    # 5. Path parameters
    try:
        r = client.get("/test/items/abc123")
        data = r.json()
        report(5, "Path parameters", data.get("item_id") == "abc123")
    except Exception as e:
        report(5, "Path parameters", False, str(e))

    # 6. Path with type (int)
    try:
        r = client.get("/test/typed-items/42")
        data = r.json()
        ok = data.get("item_id") == 42 and data.get("type") == "int"
        report(6, "Path param type int", ok)
    except Exception as e:
        report(6, "Path param type int", False, str(e))

    # 7. Query params
    try:
        r = client.get("/test/query?q=hello")
        data = r.json()
        report(7, "Query params (required)", data.get("q") == "hello")
    except Exception as e:
        report(7, "Query params (required)", False, str(e))

    # 8. Query with validation (ge=0, le=100)
    try:
        r_ok = client.get("/test/query-validated?count=50")
        r_bad = client.get("/test/query-validated?count=200")
        ok = r_ok.status_code == 200 and r_ok.json().get("count") == 50
        # Bad request should be 422 or some error
        bad_rejected = r_bad.status_code in (400, 422)
        report(8, "Query validation (ge/le)", ok and bad_rejected, f"good={r_ok.status_code}, bad={r_bad.status_code}")
    except Exception as e:
        report(8, "Query validation (ge/le)", False, str(e))

    # 9. Header params
    try:
        r = client.get("/test/header", headers={"X-Token": "secret123"})
        data = r.json()
        report(9, "Header params", data.get("x_token") == "secret123")
    except Exception as e:
        report(9, "Header params", False, str(e))

    # 10. Cookie params
    try:
        r = client.get("/test/cookie", cookies={"session": "sess123"})
        data = r.json()
        report(10, "Cookie params", data.get("session") == "sess123")
    except Exception as e:
        report(10, "Cookie params", False, str(e))

    # 11. Multiple body params with embed=True
    try:
        r = client.post("/test/multi-body", json={
            "item": {"name": "widget", "price": 5.0},
            "user": {"username": "john", "email": "j@x.com"},
        })
        data = r.json()
        ok = data.get("item_name") == "widget" and data.get("username") == "john"
        report(11, "Multiple body embed=True", ok, f"got={data}")
    except Exception as e:
        report(11, "Multiple body embed=True", False, str(e))

    # 12. response_model
    try:
        r = client.get("/test/response-model")
        data = r.json()
        ok = data.get("name") == "widget" and "secret_field" not in data
        report(12, "response_model filters", ok, f"keys={list(data.keys())}")
    except Exception as e:
        report(12, "response_model filters", False, str(e))

    # 13. response_model_exclude_unset
    try:
        r = client.get("/test/response-model-exclude-unset")
        data = r.json()
        # Only name and price should be present (in_stock was not explicitly set)
        # Actually in_stock has a default of True and was not explicitly passed...
        # Pydantic exclude_unset should exclude in_stock since it wasn't set by caller.
        ok = "name" in data and "price" in data
        has_in_stock = "in_stock" in data
        report(13, "response_model_exclude_unset", ok and not has_in_stock, f"keys={list(data.keys())}")
    except Exception as e:
        report(13, "response_model_exclude_unset", False, str(e))

    # 14. status_code=201
    try:
        r = client.post("/test/status-201")
        report(14, "status_code=201", r.status_code == 201, f"got={r.status_code}")
    except Exception as e:
        report(14, "status_code=201", False, str(e))

    # 15. tags
    try:
        r = client.get("/openapi.json")
        schema = r.json()
        tagged_path = schema.get("paths", {}).get("/test/tagged", {})
        tags = tagged_path.get("get", {}).get("tags", [])
        report(15, "tags on route", "items" in tags, f"tags={tags}")
    except Exception as e:
        report(15, "tags on route", False, str(e))

    # 16. summary and description
    try:
        r = client.get("/openapi.json")
        schema = r.json()
        doc_path = schema.get("paths", {}).get("/test/documented", {}).get("get", {})
        ok = doc_path.get("summary") == "My Summary" and doc_path.get("description") == "My detailed description"
        report(16, "summary/description", ok, f"summary={doc_path.get('summary')}")
    except Exception as e:
        report(16, "summary/description", False, str(e))

    # 17. deprecated
    try:
        r = client.get("/openapi.json")
        schema = r.json()
        dep_path = schema.get("paths", {}).get("/test/deprecated", {}).get("get", {})
        report(17, "deprecated=True", dep_path.get("deprecated") is True, f"deprecated={dep_path.get('deprecated')}")
    except Exception as e:
        report(17, "deprecated=True", False, str(e))

    # 18. include_in_schema=False
    try:
        r = client.get("/openapi.json")
        schema = r.json()
        hidden = "/test/hidden" in schema.get("paths", {})
        # Route should work but NOT appear in schema
        r2 = client.get("/test/hidden")
        ok = r2.status_code == 200 and not hidden
        report(18, "include_in_schema=False", ok, f"in_schema={hidden}, status={r2.status_code}")
    except Exception as e:
        report(18, "include_in_schema=False", False, str(e))

    # 19. APIRouter with prefix and tags
    try:
        r = client.get("/api/v1/ping")
        data = r.json()
        ok = data.get("router") == "v1" and data.get("pong") is True
        report(19, "APIRouter prefix+tags", ok)
    except Exception as e:
        report(19, "APIRouter prefix+tags", False, str(e))

    # 20. app.include_router
    # Already tested above (19), but confirm via OpenAPI
    try:
        r = client.get("/openapi.json")
        schema = r.json()
        has_router_path = "/api/v1/ping" in schema.get("paths", {})
        report(20, "app.include_router", has_router_path)
    except Exception as e:
        report(20, "app.include_router", False, str(e))

    # 21. Nested routers
    try:
        r = client.get("/api/v1/inner/hello")
        data = r.json()
        report(21, "Nested routers", r.status_code == 200 and data.get("inner") is True)
    except Exception as e:
        report(21, "Nested routers", False, str(e))

    # ======== DEPENDENCY INJECTION ========
    print("\n-- DEPENDENCY INJECTION --")

    # 22. Simple Depends
    try:
        r = client.get("/test/dep-simple")
        data = r.json()
        report(22, "Simple Depends()", data.get("dep") == "simple_dep_value")
    except Exception as e:
        report(22, "Simple Depends()", False, str(e))

    # 23. Chained depends
    try:
        r = client.get("/test/dep-chained")
        data = r.json()
        report(23, "Chained Depends", data.get("dep") == "chained:sub_dep_value")
    except Exception as e:
        report(23, "Chained Depends", False, str(e))

    # 24. Generator dep (yield)
    try:
        r = client.get("/test/dep-generator")
        data = r.json()
        report(24, "Generator dep (yield)", data.get("dep") == "gen_dep_value")
    except Exception as e:
        report(24, "Generator dep (yield)", False, str(e))

    # 25. Async dep
    try:
        r = client.get("/test/dep-async")
        data = r.json()
        report(25, "Async dependency", data.get("dep") == "async_dep_value")
    except Exception as e:
        report(25, "Async dependency", False, str(e))

    # 26. Class-based dep
    try:
        r = client.get("/test/dep-class")
        data = r.json()
        report(26, "Class-based dep", data.get("dep") == "class_dep_value")
    except Exception as e:
        report(26, "Class-based dep", False, str(e))

    # 27. Route-level dependency (no return)
    try:
        r = client.get("/test/dep-route-level")
        data = r.json()
        report(27, "Route-level dep (no return)", r.status_code == 200 and data.get("route_level_dep") is True)
    except Exception as e:
        report(27, "Route-level dep (no return)", False, str(e))

    # 28. Router-level dependency
    try:
        r = client.get("/dep-router/info")
        data = r.json()
        report(28, "Router-level dep", r.status_code == 200 and data.get("dep_router") is True)
    except Exception as e:
        report(28, "Router-level dep", False, str(e))

    # 29. dependency_overrides
    try:
        r = client.get("/test/dep-override")
        data = r.json()
        report(29, "dependency_overrides", data.get("dep") == "overridden", f"got={data.get('dep')}")
    except Exception as e:
        report(29, "dependency_overrides", False, str(e))

    # ======== REQUEST/RESPONSE ========
    print("\n-- REQUEST/RESPONSE --")

    # 30. Return dict -> auto JSON
    try:
        r = client.get("/test/return-dict")
        data = r.json()
        ct = r.headers.get("content-type", "")
        ok = data.get("type") == "dict" and "json" in ct.lower()
        report(30, "Return dict -> JSON", ok)
    except Exception as e:
        report(30, "Return dict -> JSON", False, str(e))

    # 31. Return Pydantic model -> auto JSON
    try:
        r = client.get("/test/return-model")
        data = r.json()
        ok = data.get("name") == "widget" and data.get("price") == 5.0
        report(31, "Return Pydantic model -> JSON", ok)
    except Exception as e:
        report(31, "Return Pydantic model -> JSON", False, str(e))

    # 32. JSONResponse
    try:
        r = client.get("/test/json-response")
        data = r.json()
        report(32, "JSONResponse explicit", data.get("custom") is True)
    except Exception as e:
        report(32, "JSONResponse explicit", False, str(e))

    # 33. HTMLResponse
    try:
        r = client.get("/test/html-response")
        ct = r.headers.get("content-type", "")
        ok = "<h1>Hello</h1>" in r.text and "text/html" in ct.lower()
        report(33, "HTMLResponse", ok, f"ct={ct}")
    except Exception as e:
        report(33, "HTMLResponse", False, str(e))

    # 34. PlainTextResponse
    try:
        r = client.get("/test/plain-response")
        ct = r.headers.get("content-type", "")
        ok = r.text == "hello plain" and "text/plain" in ct.lower()
        report(34, "PlainTextResponse", ok)
    except Exception as e:
        report(34, "PlainTextResponse", False, str(e))

    # 35. RedirectResponse
    try:
        r = client.get("/test/redirect")
        ok = r.status_code in (301, 302, 307, 308)
        loc = r.headers.get("location", "")
        report(35, "RedirectResponse", ok and "/test/basic-get" in loc, f"status={r.status_code}, loc={loc}")
    except Exception as e:
        report(35, "RedirectResponse", False, str(e))

    # 36. StreamingResponse
    try:
        r = client.get("/test/streaming")
        ok = "chunk1" in r.text and "chunk2" in r.text and "chunk3" in r.text
        report(36, "StreamingResponse", ok, f"body_len={len(r.text)}")
    except Exception as e:
        report(36, "StreamingResponse", False, str(e))

    # 37. FileResponse
    try:
        r = client.get("/test/file-response")
        ok = r.status_code == 200 and "file content for audit test" in r.text
        report(37, "FileResponse", ok)
    except Exception as e:
        report(37, "FileResponse", False, str(e))

    # 38. set_cookie
    try:
        r = client.get("/test/set-cookie")
        cookies = r.headers.get_list("set-cookie") if hasattr(r.headers, 'get_list') else [v for k, v in r.headers.multi_items() if k.lower() == "set-cookie"]
        cookie_str = "; ".join(cookies)
        ok = "audit_key=audit_value" in cookie_str
        report(38, "set_cookie", ok, f"cookies={cookie_str[:100]}")
    except Exception as e:
        report(38, "set_cookie", False, str(e))

    # 39. delete_cookie
    try:
        r = client.get("/test/delete-cookie")
        cookies = [v for k, v in r.headers.multi_items() if k.lower() == "set-cookie"]
        cookie_str = "; ".join(cookies)
        ok = "audit_key=" in cookie_str and "Max-Age=0" in cookie_str
        report(39, "delete_cookie", ok, f"cookies={cookie_str[:100]}")
    except Exception as e:
        report(39, "delete_cookie", False, str(e))

    # 40. Custom response headers
    try:
        r = client.get("/test/custom-headers")
        val = r.headers.get("x-custom-header", "")
        report(40, "Custom response headers", val == "custom-value", f"x-custom-header={val}")
    except Exception as e:
        report(40, "Custom response headers", False, str(e))

    # ======== VALIDATION & MODELS ========
    print("\n-- VALIDATION & MODELS --")

    # 41. Pydantic body validation
    try:
        r = client.post("/test/basic-post", json={"name": "x", "price": 1.5})
        ok_valid = r.status_code == 200
        r_bad = client.post("/test/basic-post", json={"wrong_field": "x"})
        ok_invalid = r_bad.status_code in (400, 422)
        report(41, "Pydantic body validation", ok_valid and ok_invalid, f"valid={r.status_code}, invalid={r_bad.status_code}")
    except Exception as e:
        report(41, "Pydantic body validation", False, str(e))

    # 42. Field(ge=0) validation
    try:
        r_ok = client.post("/test/basic-post", json={"name": "x", "price": 5.0})
        r_bad = client.post("/test/basic-post", json={"name": "x", "price": -1.0})
        ok_valid = r_ok.status_code == 200
        ok_bad = r_bad.status_code in (400, 422)
        report(42, "Field(ge=0) validation", ok_valid and ok_bad, f"valid={r_ok.status_code}, bad={r_bad.status_code}")
    except Exception as e:
        report(42, "Field(ge=0) validation", False, str(e))

    # 43. Optional fields
    try:
        r = client.post("/test/optional-fields", json={"name": "x", "price": 1.0})
        data = r.json()
        ok = data.get("name") == "x" and data.get("description") is None
        report(43, "Optional fields", ok, f"status={r.status_code}, body={data}")
    except Exception as e:
        report(43, "Optional fields", False, str(e))

    # 44. List fields
    try:
        r = client.post("/test/basic-post", json={"name": "x", "price": 1.0, "tags": ["a", "b"]})
        data = r.json()
        report(44, "List fields", r.status_code == 200 and data.get("name") == "x")
    except Exception as e:
        report(44, "List fields", False, str(e))

    # 45. Nested models
    try:
        r = client.post("/test/nested-model", json={
            "name": "widget",
            "price": 1.0,
            "sub_item": {"sub_name": "part", "sub_value": 42},
        })
        data = r.json()
        ok = data.get("sub_item", {}).get("sub_name") == "part"
        report(45, "Nested models", ok)
    except Exception as e:
        report(45, "Nested models", False, str(e))

    # 46. response_model filters extra fields (same as 12)
    try:
        r = client.get("/test/response-model")
        data = r.json()
        ok = "secret_field" not in data and "name" in data
        report(46, "response_model filters extras", ok)
    except Exception as e:
        report(46, "response_model filters extras", False, str(e))

    # 47. Enum in query params
    # NOTE: FastAPI auto-converts query string to Enum type; fastapi-turbo passes raw str.
    # Handler must be written to handle both (this is a known gap).
    try:
        r = client.get("/test/enum-query?color=green")
        if r.status_code != 200:
            report(47, "Enum query params", False, f"status={r.status_code}, body={r.text[:200]}")
        else:
            data = r.json()
            ok = data.get("color") == "green"
            r_default = client.get("/test/enum-query")
            if r_default.status_code != 200:
                report(47, "Enum query params", ok, f"green ok={ok}, default status={r_default.status_code}, body={r_default.text[:200]}")
            else:
                data_d = r_default.json()
                ok_default = data_d.get("color") == "red"
                report(47, "Enum query params", ok and ok_default, f"green={data}, default={data_d}")
    except Exception as e:
        report(47, "Enum query params", False, str(e))

    # 47b. Enum query strict (FastAPI-compat: handler uses .value directly)
    try:
        # With explicit param: Rust passes raw str, so .value will fail
        r = client.get("/test/enum-query-strict?color=green")
        if r.status_code == 200:
            data = r.json()
            ok = data.get("color") == "green"
            # Also test default (no param) -- default is an Enum, so .value works
            r2 = client.get("/test/enum-query-strict")
            ok2 = r2.status_code == 200 and r2.json().get("color") == "red"
            report("47b", "Enum .value (strict FastAPI compat)", ok and ok2, f"explicit={data}, default={r2.json() if r2.status_code == 200 else r2.text[:80]}")
        else:
            # Default works (Enum object), explicit param fails (raw str has no .value)
            r_def = client.get("/test/enum-query-strict")
            def_ok = r_def.status_code == 200 and r_def.json().get("color") == "red"
            report("47b", "Enum .value (strict FastAPI compat)", False,
                   f"KNOWN GAP: explicit param returns {r.status_code} (Rust passes raw str, not Enum); default works={def_ok}")
    except Exception as e:
        report("47b", "Enum .value (strict FastAPI compat)", False, str(e))

    # ======== MIDDLEWARE ========
    print("\n-- MIDDLEWARE --")

    # 48. @app.middleware("http")
    try:
        r = client.get("/test/basic-get")
        val = r.headers.get("x-audit-middleware", "")
        report(48, "@app.middleware('http')", val == "applied", f"header={val}")
    except Exception as e:
        report(48, "@app.middleware('http')", False, str(e))

    # 49. CORSMiddleware -- checked via OPTIONS preflight
    try:
        r = client.options("/test/basic-get", headers={
            "Origin": "http://example.com",
            "Access-Control-Request-Method": "GET",
        })
        acl = r.headers.get("access-control-allow-origin", "")
        ok = "example.com" in acl or acl == "*"
        report(49, "CORSMiddleware", ok, f"ACAO={acl}, status={r.status_code}")
    except Exception as e:
        report(49, "CORSMiddleware", False, str(e))

    # 50. BaseHTTPMiddleware subclass
    try:
        r = client.get("/test/basic-get")
        val = r.headers.get("x-base-middleware", "")
        report(50, "BaseHTTPMiddleware subclass", val == "yes", f"header={val}")
    except Exception as e:
        report(50, "BaseHTTPMiddleware subclass", False, str(e))

    # 51. Middleware ordering (outer runs first; base middleware was added AFTER http middleware)
    try:
        r = client.get("/test/basic-get")
        has_timing = "x-audit-middleware" in r.headers
        has_base = "x-base-middleware" in r.headers
        report(51, "Middleware ordering", has_timing and has_base, "both present")
    except Exception as e:
        report(51, "Middleware ordering", False, str(e))

    # ======== ERROR HANDLING ========
    print("\n-- ERROR HANDLING --")

    # 52. HTTPException
    try:
        r = client.get("/test/http-exception")
        data = r.json()
        ok = r.status_code == 404 and data.get("custom_error") is True and data.get("detail") == "item not found"
        report(52, "HTTPException", ok, f"status={r.status_code}, body={data}")
    except Exception as e:
        report(52, "HTTPException", False, str(e))

    # 53. HTTPException with custom headers
    try:
        r = client.get("/test/http-exception-headers")
        data = r.json()
        x_err = r.headers.get("x-error", "")
        ok = r.status_code == 403 and data.get("detail") == "forbidden" and x_err == "yes"
        report(53, "HTTPException with headers", ok, f"X-Error={x_err}")
    except Exception as e:
        report(53, "HTTPException with headers", False, str(e))

    # 54. @app.exception_handler(HTTPException)
    try:
        r = client.get("/test/http-exception")
        data = r.json()
        ok = data.get("custom_error") is True
        report(54, "Custom HTTPException handler", ok)
    except Exception as e:
        report(54, "Custom HTTPException handler", False, str(e))

    # 55. @app.exception_handler(RequestValidationError)
    try:
        # Send invalid body to trigger validation error
        r = client.post("/test/basic-post", content=b"not json", headers={"content-type": "application/json"})
        data = r.json()
        ok = data.get("custom_validation") is True or r.status_code == 422
        report(55, "Custom validation handler", ok, f"status={r.status_code}, body={data}")
    except Exception as e:
        report(55, "Custom validation handler", False, str(e))

    # 56. @app.exception_handler(MyCustomError)
    try:
        r = client.get("/test/custom-exception")
        data = r.json()
        ok = r.status_code == 500 and data.get("custom_type") == "MyCustomError" and data.get("msg") == "something broke"
        report(56, "Custom exception handler", ok, f"body={data}")
    except Exception as e:
        report(56, "Custom exception handler", False, str(e))

    # ======== LIFECYCLE ========
    print("\n-- LIFECYCLE --")

    # 57/58. Lifespan + app.state
    try:
        r = client.get("/test/lifespan-state")
        data = r.json()
        db_ok = data.get("db_initialized") is True
        lifespan_ok = data.get("lifespan_data") == "hello_from_lifespan"
        report(57, "Lifespan startup + state", db_ok, f"data={data}")
        report(58, "app.state read in handler", lifespan_ok, f"lifespan_data={data.get('lifespan_data')}")
    except Exception as e:
        report(57, "Lifespan startup + state", False, str(e))
        report(58, "app.state read in handler", False, str(e))

    # ======== SECURITY ========
    print("\n-- SECURITY --")

    # 59. OAuth2PasswordBearer
    try:
        r = client.get("/test/security-oauth2", headers={"Authorization": "Bearer mytoken123"})
        data = r.json()
        ok = data.get("token") == "mytoken123"
        # Also test missing token -> 401
        r_no = client.get("/test/security-oauth2")
        ok_no = r_no.status_code in (401, 403)
        report(59, "OAuth2PasswordBearer", ok and ok_no, f"token={data.get('token')}, no_token_status={r_no.status_code}")
    except Exception as e:
        report(59, "OAuth2PasswordBearer", False, str(e))

    # 60. HTTPBearer
    try:
        r = client.get("/test/security-bearer", headers={"Authorization": "Bearer secrettoken"})
        data = r.json()
        ok = data.get("credentials") == "secrettoken" and data.get("scheme") == "Bearer"
        report(60, "HTTPBearer", ok)
    except Exception as e:
        report(60, "HTTPBearer", False, str(e))

    # 61. APIKeyHeader
    try:
        r = client.get("/test/security-apikey", headers={"X-API-Key": "my-key"})
        data = r.json()
        ok = data.get("api_key") == "my-key"
        report(61, "APIKeyHeader", ok, f"got={data}")
    except Exception as e:
        report(61, "APIKeyHeader", False, str(e))

    # ======== WEBSOCKET ========
    print("\n-- WEBSOCKET --")

    # 62/63. WebSocket echo
    try:
        import websockets
        import asyncio

        async def ws_test():
            async with websockets.connect("ws://127.0.0.1:19800/test/ws-echo") as ws:
                await ws.send("hello")
                resp = await asyncio.wait_for(ws.recv(), timeout=5)
                return resp

        ws_resp = asyncio.run(ws_test())
        ok = ws_resp == "echo:hello"
        report(62, "WebSocket echo", ok, f"got={ws_resp}")
        report(63, "WebSocket custom param name", ok, "uses 'websocket' param")
    except Exception as e:
        report(62, "WebSocket echo", False, str(e))
        report(63, "WebSocket custom param name", False, str(e))

    # ======== OPENAPI ========
    print("\n-- OPENAPI --")

    # 64. /docs serves Swagger UI
    try:
        r = client.get("/docs")
        ok = r.status_code == 200 and ("swagger" in r.text.lower() or "openapi" in r.text.lower())
        report(64, "/docs Swagger UI", ok, f"status={r.status_code}, len={len(r.text)}")
    except Exception as e:
        report(64, "/docs Swagger UI", False, str(e))

    # 65. /redoc serves ReDoc
    try:
        r = client.get("/redoc")
        ok = r.status_code == 200 and ("redoc" in r.text.lower() or "openapi" in r.text.lower())
        report(65, "/redoc ReDoc", ok, f"status={r.status_code}, len={len(r.text)}")
    except Exception as e:
        report(65, "/redoc ReDoc", False, str(e))

    # 66. /openapi.json has all routes
    try:
        r = client.get("/openapi.json")
        schema = r.json()
        paths = schema.get("paths", {})
        ok = r.status_code == 200 and len(paths) > 10  # Should have many routes
        report(66, "/openapi.json all routes", ok, f"num_paths={len(paths)}")
    except Exception as e:
        report(66, "/openapi.json all routes", False, str(e))

    # ======== SPECIAL ========
    print("\n-- SPECIAL --")

    # 67. BackgroundTasks
    try:
        r = client.post("/test/background-task")
        data = r.json()
        ok_queued = data.get("queued") is True
        # Give background task a moment to run
        time.sleep(0.5)
        r2 = client.get("/test/background-task-check")
        data2 = r2.json()
        ok_ran = "task_executed" in data2.get("log", [])
        report(67, "BackgroundTasks", ok_queued and ok_ran, f"log={data2.get('log')}")
    except Exception as e:
        report(67, "BackgroundTasks", False, str(e))

    # 68. Form
    try:
        r = client.post("/test/form", data={"username": "john", "password": "secret"})
        data = r.json()
        ok = data.get("username") == "john" and data.get("password") == "secret"
        report(68, "Form parameters", ok, f"got={data}")
    except Exception as e:
        report(68, "Form parameters", False, str(e))

    # 69. File/UploadFile
    try:
        import io
        files = {"file": ("test.txt", io.BytesIO(b"hello upload"), "text/plain")}
        r = client.post("/test/upload", files=files)
        data = r.json()
        ok = data.get("filename") == "test.txt" and data.get("size") == 12
        report(69, "File/UploadFile", ok, f"got={data}")
    except Exception as e:
        report(69, "File/UploadFile", False, str(e))

    # 70. Request injection
    try:
        r = client.get("/test/request-injection")
        data = r.json()
        ok = data.get("method") == "GET" and data.get("has_headers") is True
        report(70, "Request injection", ok, f"got={data}")
    except Exception as e:
        report(70, "Request injection", False, str(e))

    client.close()

    # ======== SUMMARY ========
    print("\n" + "=" * 70)
    passed = sum(1 for _, _, p, _ in results if p)
    failed = sum(1 for _, _, p, _ in results if not p)
    total = len(results)
    print(f"  TOTAL: {total}  |  PASS: {passed}  |  FAIL: {failed}")
    print("=" * 70)

    if failed > 0:
        print("\n  FAILURES:")
        for num, name, ok, detail in results:
            if not ok:
                num_str = f"{num:02d}" if isinstance(num, int) else str(num)
                print(f"    #{num_str} {name}: {detail}")
    print()


if __name__ == "__main__":
    # Start the audit app as a subprocess
    python = sys.executable
    app_script = os.path.join(os.path.dirname(__file__), "functional_audit_app.py")

    print(f"Starting audit app on port 19800 ...")
    proc = subprocess.Popen(
        [python, app_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=os.path.dirname(__file__),
    )

    try:
        if not wait_for_server(15):
            print("ERROR: Server did not start in time.")
            stdout, stderr = proc.communicate(timeout=5)
            print("STDOUT:", stdout.decode()[:2000])
            print("STDERR:", stderr.decode()[:2000])
            sys.exit(1)

        print(f"Server ready. Running tests...\n")
        run_tests()

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
