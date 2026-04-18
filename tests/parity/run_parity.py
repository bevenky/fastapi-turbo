#!/usr/bin/env python3
"""Behavioral parity runner: start same app on FastAPI + fastapi-rs, compare responses.

Usage:
    python tests/parity/run_parity.py [--pattern P001] [--fastapi-only] [--rs-only] [--verbose]
"""
import argparse
import base64
import json
import os
import signal
import socket
import subprocess
import sys
import time
import traceback

import httpx

PYTHON = sys.executable
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(TEST_DIR))


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_for_server(port, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


def start_fastapi(port):
    """Start the parity app under real FastAPI/uvicorn."""
    proc = subprocess.Popen(
        [PYTHON, "-c", f"""
import sys, os
os.environ["FASTAPI_RS_NO_SHIM"] = "1"
sys.path.insert(0, "{TEST_DIR}")
import uvicorn
from parity_app import app
uvicorn.run(app, host="127.0.0.1", port={port}, log_level="error")
"""],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    return proc


def start_fastapi_rs(port):
    """Start the parity app under fastapi-rs."""
    proc = subprocess.Popen(
        [PYTHON, "-c", f"""
import sys, os
# Must import fastapi_rs.compat BEFORE importing the app so the shims
# redirect `from fastapi import ...` to fastapi_rs.
import fastapi_rs.compat
fastapi_rs.compat.install()
sys.path.insert(0, "{TEST_DIR}")
from parity_app import app
app.run("127.0.0.1", {port})
"""],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    return proc


# ── Test definitions ─────────────────────────────────────────────

def compare_json(fa_r, rs_r):
    """Compare two responses: status code and JSON body."""
    issues = []
    if fa_r.status_code != rs_r.status_code:
        issues.append(f"status: FA={fa_r.status_code} RS={rs_r.status_code}")
    try:
        fa_j = fa_r.json()
    except Exception:
        fa_j = fa_r.text
    try:
        rs_j = rs_r.json()
    except Exception:
        rs_j = rs_r.text
    if fa_j != rs_j:
        issues.append(f"body: FA={_trunc(fa_j)} RS={_trunc(rs_j)}")
    return issues


def compare_status(fa_r, rs_r):
    """Compare only status codes."""
    if fa_r.status_code != rs_r.status_code:
        return [f"status: FA={fa_r.status_code} RS={rs_r.status_code}"]
    return []


def compare_status_and_text(fa_r, rs_r):
    """Compare status + text body."""
    issues = []
    if fa_r.status_code != rs_r.status_code:
        issues.append(f"status: FA={fa_r.status_code} RS={rs_r.status_code}")
    if fa_r.text != rs_r.text:
        issues.append(f"text: FA={_trunc(fa_r.text)} RS={_trunc(rs_r.text)}")
    return issues


def compare_redirect(fa_r, rs_r):
    """Compare redirect: status and location header."""
    issues = []
    if fa_r.status_code != rs_r.status_code:
        issues.append(f"status: FA={fa_r.status_code} RS={rs_r.status_code}")
    fa_loc = fa_r.headers.get("location", "")
    rs_loc = rs_r.headers.get("location", "")
    if fa_loc != rs_loc:
        issues.append(f"location: FA={fa_loc} RS={rs_loc}")
    return issues


def _trunc(val, maxlen=120):
    s = str(val)
    if len(s) > maxlen:
        return s[:maxlen] + "..."
    return s


def build_tests():
    """Return list of (pattern_name, method, path, kwargs, comparator)."""
    item_body = {"name": "widget", "price": 9.99, "description": "a widget"}
    tests = []

    def t(name, method, path, comparator=compare_json, **kwargs):
        tests.append((name, method, path, kwargs, comparator))

    # ── Routing (1-30) ──
    t("P001", "get", "/p001-basic-get")
    t("P002", "post", "/p002-basic-post", json=item_body)
    t("P003", "put", "/p003-basic-put", json=item_body)
    t("P004", "patch", "/p004-basic-patch", json=item_body)
    t("P005", "delete", "/p005-basic-delete")
    t("P006", "get", "/p006-path-int/42")
    t("P007", "get", "/p007-path-str/hello")
    t("P008", "get", "/p008-path-float/3.14")
    t("P009", "get", "/p009-query-required?q=test")
    t("P010", "get", "/p010-query-default")
    t("P010b", "get", "/p010-query-default?q=world")
    t("P011", "get", "/p011-query-optional")
    t("P011b", "get", "/p011-query-optional?q=found")
    t("P012", "get", "/p012-query-int?n=5")
    t("P013", "get", "/p013-query-bool?flag=true")
    t("P014", "get", "/p014-query-list?items=a&items=b")
    t("P015", "get", "/p015-multi-query?skip=5&limit=20")
    t("P016", "get", "/p016-header")
    t("P017", "get", "/p017-cookie")
    t("P018", "get", "/p018-path-query/7?q=hello")
    t("P019", "post", "/p019-body-model", json=item_body)
    t("P020", "post", "/p020-body-embed", json={"item": item_body})
    t("P021", "post", "/p021-multi-body", json={"item": item_body, "extra": "stuff"})
    t("P022", "post", "/p022-form", data={"username": "admin", "password": "secret"})
    t("P023", "post", "/p023-file", files={"file": ("test.txt", b"hello world")})
    t("P024", "post", "/p024-uploadfile", files={"file": ("test.txt", b"hello world")})
    t("P025", "post", "/p025-form-file", data={"name": "doc"}, files={"file": ("test.txt", b"hello world")})
    t("P026", "get", "/p026-response-model")
    t("P027", "get", "/p027-response-model-exclude")
    t("P028", "get", "/p028-status-code")
    t("P029", "get", "/p029-deprecated")
    t("P030", "get", "/p030-tags")

    # ── Response types (31-50) ──
    t("P031", "get", "/p031-json")
    t("P032", "get", "/p032-json-response")
    t("P033", "get", "/p033-html", comparator=compare_status_and_text)
    t("P034", "get", "/p034-plain", comparator=compare_status_and_text)
    t("P035", "get", "/p035-redirect", comparator=compare_redirect)
    t("P036", "get", "/p036-stream", comparator=compare_status_and_text)
    t("P037", "get", "/p037-file", comparator=compare_status_and_text)
    t("P038", "get", "/p038-orjson")
    t("P039", "get", "/p039-custom-headers")
    t("P040", "get", "/p040-set-cookie")
    t("P041", "get", "/p041-delete-cookie")
    t("P042", "get", "/p042-status-204", comparator=compare_status)
    t("P043", "get", "/p043-bytes", comparator=compare_status_and_text)
    t("P044", "post", "/p044-return-model", json=item_body)
    t("P045", "get", "/p045-none")
    t("P046", "get", "/p046-string")
    t("P047", "get", "/p047-int")
    t("P048", "get", "/p048-list")
    t("P049", "get", "/p049-nested-dict")
    t("P050", "get", "/p050-decimal")

    # ── Validation (51-70) ──
    t("P051-pass", "get", "/p051-query-ge?n=5")
    t("P051-fail", "get", "/p051-query-ge?n=-1", comparator=compare_status)
    t("P052-pass", "get", "/p052-query-le?n=5")
    t("P052-fail", "get", "/p052-query-le?n=20", comparator=compare_status)
    t("P053", "get", "/p053-query-gt-lt?n=5")
    t("P054", "post", "/p054-body-validation", json={"bad": "data"}, comparator=compare_status)
    t("P055", "post", "/p055-nested-validation", json={"child": {"value": "not_int"}, "label": "x"}, comparator=compare_status)
    t("P056", "get", "/p056-enum-query?color=red")
    t("P057", "get", "/p057-regex-query?code=ABC")
    t("P057-fail", "get", "/p057-regex-query?code=abc", comparator=compare_status)
    t("P058", "get", "/p058-min-max-length?s=hi")
    t("P058-fail", "get", "/p058-min-max-length?s=", comparator=compare_status)
    t("P059", "post", "/p059-optional-fields", json={"required_field": "yes"})
    t("P060", "post", "/p060-list-field", json={"tags": ["a", "b", "c"]})
    t("P061", "post", "/p061-nested-model", json={"child": {"value": 42}, "label": "test"})
    t("P062", "post", "/p062-field-alias", json={"itemName": "widget"})
    t("P063", "post", "/p063-field-description", json={"value": 10})
    t("P064", "post", "/p064-field-example", json={"count": 5})
    t("P065", "post", "/p065-field-default", json={})
    t("P066-pass", "post", "/p066-field-ge", json={"amount": 5})
    t("P066-fail", "post", "/p066-field-ge", json={"amount": -1}, comparator=compare_status)
    t("P067", "post", "/p067-discriminated-union", json={"kind": "a", "a_val": 42})
    t("P068", "get", "/p068-typed-path/99")

    # ── Dependency Injection (71-100) ──
    t("P071", "get", "/p071-simple-dep")
    t("P072", "get", "/p072-chained-dep")
    t("P073", "get", "/p073-generator-dep")
    t("P074", "get", "/p074-async-dep")
    t("P075", "get", "/p075-class-dep")
    t("P076", "get", "/p076-dep-no-return")
    # P077 dep override is tested differently — skip compare (behavior is app-level)
    t("P077", "get", "/p077-dep-override")
    t("P078", "get", "/p078-dep-with-query?q=custom")
    t("P079", "get", "/p079-dep-with-header")
    t("P080", "get", "/p080-dep-with-request")
    t("P081", "get", "/p081-security-oauth2", headers={"Authorization": "Bearer mytoken123"})
    t("P081-noauth", "get", "/p081-security-oauth2", comparator=compare_status)
    t("P082", "get", "/p082-security-bearer", headers={"Authorization": "Bearer xyz"})
    t("P083", "get", "/p083-security-apikey", headers={"X-API-Key": "secret123"})
    t("P084", "get", "/p084-security-basic", headers={"Authorization": "Basic " + base64.b64encode(b"user:pass").decode()})
    t("P085", "get", "/p085-security-scopes", headers={"Authorization": "Bearer scopedtoken"})
    t("P086", "post", "/p086-oauth2-form", data={"username": "admin", "password": "secret", "scope": "", "grant_type": "password"})
    t("P087", "get", "/p087-dep-cached")
    t("P088", "get", "/p088-dep-async-generator")
    t("P089", "get", "/p089-nested-deps-3-deep")
    t("P090", "get", "/p090-dep-exception", comparator=compare_status)
    t("P091", "get", "/p091-router-dep")
    t("P092", "get", "/p092-include-router-dep")
    t("P093", "get", "/p093-dep-background")
    t("P094", "get", "/p094-dep-response")
    t("P095", "get", "/p095-dep-websocket")
    t("P096", "get", "/p096-multiple-deps")
    t("P097", "get", "/p097-dep-default")
    t("P098", "get", "/p098-annotated-dep")
    t("P099", "get", "/p099-security-auto-error")
    t("P099-with-token", "get", "/p099-security-auto-error", headers={"Authorization": "Bearer tok99"})
    t("P100", "get", "/p100-dep-yield-cleanup")

    return tests


def run_test(client, fa_port, rs_port, name, method, path, kwargs, comparator):
    """Run a single test: hit both servers, compare."""
    fa_url = f"http://127.0.0.1:{fa_port}{path}"
    rs_url = f"http://127.0.0.1:{rs_port}{path}"

    # For redirects, don't follow
    follow = "redirect" not in name.lower() and "redirect" not in path.lower()

    try:
        fa_r = getattr(client, method)(fa_url, follow_redirects=follow, **kwargs)
    except Exception as e:
        return [f"FA request error: {e}"]

    try:
        rs_r = getattr(client, method)(rs_url, follow_redirects=follow, **kwargs)
    except Exception as e:
        return [f"RS request error: {e}"]

    return comparator(fa_r, rs_r)


def main():
    parser = argparse.ArgumentParser(description="Run behavioral parity tests")
    parser.add_argument("--pattern", "-p", help="Run only this pattern (e.g., P001)")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--fastapi-only", action="store_true", help="Only start FastAPI")
    parser.add_argument("--rs-only", action="store_true", help="Only start fastapi-rs")
    args = parser.parse_args()

    fa_port = free_port()
    rs_port = free_port()

    print(f"Starting FastAPI on :{fa_port} ...")
    fa_proc = start_fastapi(fa_port)

    print(f"Starting fastapi-rs on :{rs_port} ...")
    rs_proc = start_fastapi_rs(rs_port)

    try:
        if not wait_for_server(fa_port):
            stderr = fa_proc.stderr.read().decode() if fa_proc.stderr else ""
            print(f"FATAL: FastAPI did not start on :{fa_port}")
            if stderr:
                print(f"  stderr: {stderr[:500]}")
            return 1

        if not wait_for_server(rs_port):
            stderr = rs_proc.stderr.read().decode() if rs_proc.stderr else ""
            print(f"FATAL: fastapi-rs did not start on :{rs_port}")
            if stderr:
                print(f"  stderr: {stderr[:500]}")
            return 1

        print("Both servers ready. Running tests...\n")

        tests = build_tests()
        if args.pattern:
            tests = [(n, m, p, kw, c) for n, m, p, kw, c in tests if n.upper().startswith(args.pattern.upper())]

        passed = 0
        failed = 0
        failures = []

        with httpx.Client(timeout=5.0) as client:
            for name, method, path, kwargs, comparator in tests:
                issues = run_test(client, fa_port, rs_port, name, method, path, kwargs, comparator)
                if issues:
                    failed += 1
                    failures.append((name, issues))
                    status_char = "FAIL"
                else:
                    passed += 1
                    status_char = "PASS"

                if args.verbose or issues:
                    print(f"  {status_char}: {name:20s} {method.upper():6s} {path}")
                    for issue in issues:
                        print(f"         {issue}")

        print(f"\n{'=' * 60}")
        print(f"PASS: {passed}/{passed + failed}")
        print(f"FAIL: {failed}")
        if failures:
            for name, issues in failures:
                for issue in issues:
                    print(f"  {name}: {issue}")
        print(f"{'=' * 60}")
        return 0 if failed == 0 else 1

    finally:
        for proc in [fa_proc, rs_proc]:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
