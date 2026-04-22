#!/usr/bin/env python3
"""Deep OpenAPI schema parity runner.

Starts the deep-schema parity app under BOTH stock FastAPI (uvicorn) and
fastapi-rs, fetches both `/openapi.json` documents, and runs a large
collection of per-field structural assertions.

Each test targets ONE structural property (e.g. a single operationId, a
single parameter's `in`, a single property's `type`, a single security
scheme's `bearerFormat`). Tests are generated programmatically from the
reference schema so coverage scales with the app surface.

Usage:
    cd /Users/venky/tech/jamun
    source /Users/venky/tech/jamun_env/bin/activate
    python tests/parity/run_deep_schema_parity.py
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import threading
from collections import Counter, defaultdict
from typing import Any, Callable, Dict, List, Tuple

import httpx

PYTHON = sys.executable
TEST_DIR = os.path.dirname(os.path.abspath(__file__))

FA_PORT = 29500
RS_PORT = 29501

MAX_TESTS = 500


# ── Server startup ───────────────────────────────────────────────────

def start_fastapi(port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [PYTHON, "-c", f"""
import sys, os
os.environ['FASTAPI_RS_NO_SHIM'] = '1'
sys.path.insert(0, {TEST_DIR!r})
import uvicorn
from parity_app_deep_schema import app
uvicorn.run(app, host='127.0.0.1', port={port}, log_level='error')
"""],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def start_fastapi_rs(port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [PYTHON, "-c", f"""
import sys, os
import fastapi_rs.compat
fastapi_rs.compat.install()
sys.path.insert(0, {TEST_DIR!r})
from parity_app_deep_schema import app
app.run('127.0.0.1', {port})
"""],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def wait_for_openapi(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/openapi.json", timeout=1.5)
            if r.status_code == 200:
                r.json()
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def fetch_openapi(port: int) -> dict:
    return httpx.get(f"http://127.0.0.1:{port}/openapi.json", timeout=10.0).json()


# ── Helpers ──────────────────────────────────────────────────────────

def _short(val: Any, limit: int = 120) -> str:
    s = json.dumps(val, sort_keys=True, default=str) if not isinstance(val, str) else val
    if len(s) > limit:
        return s[:limit] + "..."
    return s


def _resolve_ref(schema_doc: dict, ref: str) -> Any:
    """Resolve a local `$ref` like `#/components/schemas/Item`."""
    if not ref.startswith("#/"):
        return None
    cur: Any = schema_doc
    for part in ref[2:].split("/"):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


class TestResult:
    def __init__(self, name: str, category: str, passed: bool, detail: str = ""):
        self.name = name
        self.category = category
        self.passed = passed
        self.detail = detail


def _mk_test(
    name: str, category: str, fa_val: Any, rs_val: Any,
    results: list, ordered: bool = True,
) -> None:
    """Compare two values with JSON equality and record the result."""
    try:
        if ordered:
            passed = fa_val == rs_val
        else:
            passed = _unordered_eq(fa_val, rs_val)
    except Exception as e:
        results.append(TestResult(name, category, False, f"compare error: {e}"))
        return
    if passed:
        results.append(TestResult(name, category, True))
    else:
        detail = f"FA={_short(fa_val)} RS={_short(rs_val)}"
        results.append(TestResult(name, category, False, detail))


def _unordered_eq(a: Any, b: Any) -> bool:
    """Structural equality that ignores list order (used for tags, required, etc.)."""
    if isinstance(a, list) and isinstance(b, list):
        try:
            return sorted(a, key=lambda v: json.dumps(v, sort_keys=True, default=str)) == \
                   sorted(b, key=lambda v: json.dumps(v, sort_keys=True, default=str))
        except Exception:
            return a == b
    return a == b


# ── Test generators ──────────────────────────────────────────────────

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head", "trace")


def gen_path_operation_tests(fa: dict, rs: dict, results: list,
                              budget: Dict[str, int]) -> None:
    """Generate per-(path, method, property) structural tests."""
    fa_paths = fa.get("paths", {})
    rs_paths = rs.get("paths", {})

    # First: path existence
    fa_path_set = set(fa_paths.keys())
    rs_path_set = set(rs_paths.keys())
    for p in sorted(fa_path_set):
        name = f"path_exists[{p}]"
        passed = p in rs_path_set
        detail = "" if passed else "FR missing path"
        results.append(TestResult(name, "path_exists", passed, detail))
        if budget_exceeded(results, budget):
            return

    # For each path/method in FA, compare operation-level properties
    for path in sorted(fa_paths.keys()):
        fa_ops = fa_paths[path]
        rs_ops = rs_paths.get(path, {})
        if not isinstance(fa_ops, dict):
            continue
        for method in HTTP_METHODS:
            if method not in fa_ops:
                continue
            fa_op = fa_ops[method]
            rs_op = rs_ops.get(method, {}) if isinstance(rs_ops, dict) else {}

            # method existence
            _mk_test(
                f"method_exists[{method.upper()} {path}]",
                "method_exists",
                True, bool(rs_op) and isinstance(rs_op, dict),
                results,
            )
            if budget_exceeded(results, budget):
                return

            # Compare scalar op props
            for prop in ("operationId", "summary", "description", "deprecated"):
                fa_val = fa_op.get(prop, None)
                rs_val = rs_op.get(prop, None) if isinstance(rs_op, dict) else None
                # Normalize missing vs None — treat the same
                if fa_val is None and rs_val is None:
                    continue
                _mk_test(
                    f"op.{prop}[{method.upper()} {path}]",
                    f"op.{prop}",
                    fa_val, rs_val, results,
                )
                if budget_exceeded(results, budget):
                    return

            # Tags — order-insensitive
            fa_tags = fa_op.get("tags")
            rs_tags = rs_op.get("tags") if isinstance(rs_op, dict) else None
            if fa_tags is not None or rs_tags is not None:
                _mk_test(
                    f"op.tags[{method.upper()} {path}]",
                    "op.tags",
                    fa_tags, rs_tags, results, ordered=False,
                )
                if budget_exceeded(results, budget):
                    return

            # Parameters
            fa_params = fa_op.get("parameters", []) or []
            rs_params = rs_op.get("parameters", []) if isinstance(rs_op, dict) else []
            rs_params = rs_params or []
            _mk_test(
                f"op.parameters.count[{method.upper()} {path}]",
                "op.parameters.count",
                len(fa_params), len(rs_params), results,
            )
            if budget_exceeded(results, budget):
                return
            # Index rs params by (name, in) for lookup
            rs_param_index = {}
            for rp in rs_params:
                if isinstance(rp, dict):
                    rs_param_index[(rp.get("name"), rp.get("in"))] = rp
            for fp in fa_params:
                if not isinstance(fp, dict):
                    continue
                pname = fp.get("name")
                pin = fp.get("in")
                key = (pname, pin)
                rp = rs_param_index.get(key, {})
                # name + in match (lookup)
                _mk_test(
                    f"param.exists[{method.upper()} {path}][{pin}:{pname}]",
                    "param.exists",
                    True, bool(rp), results,
                )
                if budget_exceeded(results, budget):
                    return
                for attr in ("required", "description", "deprecated"):
                    fa_a = fp.get(attr)
                    rs_a = rp.get(attr) if isinstance(rp, dict) else None
                    if fa_a is None and rs_a is None:
                        continue
                    _mk_test(
                        f"param.{attr}[{method.upper()} {path}][{pin}:{pname}]",
                        f"param.{attr}",
                        fa_a, rs_a, results,
                    )
                    if budget_exceeded(results, budget):
                        return
                # Schema subset: type, format, enum, minimum, maximum, minLength,
                # maxLength, pattern, items
                fa_schema = fp.get("schema") or {}
                rs_schema = rp.get("schema") if isinstance(rp, dict) else None
                rs_schema = rs_schema or {}
                for sattr in (
                    "type", "format", "enum", "minimum", "maximum", "exclusiveMinimum",
                    "exclusiveMaximum", "minLength", "maxLength", "pattern", "default",
                ):
                    if sattr not in fa_schema and sattr not in rs_schema:
                        continue
                    _mk_test(
                        f"param.schema.{sattr}[{method.upper()} {path}][{pin}:{pname}]",
                        f"param.schema.{sattr}",
                        fa_schema.get(sattr), rs_schema.get(sattr), results,
                        ordered=False if sattr == "enum" else True,
                    )
                    if budget_exceeded(results, budget):
                        return
                # For array params, peek at items.type
                if isinstance(fa_schema.get("items"), dict) or isinstance(rs_schema.get("items"), dict):
                    fa_items = fa_schema.get("items") or {}
                    rs_items = rs_schema.get("items") or {}
                    _mk_test(
                        f"param.schema.items.type[{method.upper()} {path}][{pin}:{pname}]",
                        "param.schema.items.type",
                        fa_items.get("type"), rs_items.get("type"), results,
                    )
                    if budget_exceeded(results, budget):
                        return

            # requestBody
            fa_rb = fa_op.get("requestBody")
            rs_rb = rs_op.get("requestBody") if isinstance(rs_op, dict) else None
            if fa_rb is not None or rs_rb is not None:
                _mk_test(
                    f"op.requestBody.exists[{method.upper()} {path}]",
                    "op.requestBody.exists",
                    fa_rb is not None, rs_rb is not None, results,
                )
                if budget_exceeded(results, budget):
                    return
                if isinstance(fa_rb, dict) and isinstance(rs_rb, dict):
                    _mk_test(
                        f"op.requestBody.required[{method.upper()} {path}]",
                        "op.requestBody.required",
                        fa_rb.get("required"), rs_rb.get("required"), results,
                    )
                    if budget_exceeded(results, budget):
                        return
                    fa_content = fa_rb.get("content", {}) or {}
                    rs_content = rs_rb.get("content", {}) or {}
                    _mk_test(
                        f"op.requestBody.content.mimes[{method.upper()} {path}]",
                        "op.requestBody.content.mimes",
                        sorted(fa_content.keys()),
                        sorted(rs_content.keys()),
                        results,
                    )
                    if budget_exceeded(results, budget):
                        return
                    for mime in fa_content.keys():
                        fa_media = fa_content.get(mime, {}) or {}
                        rs_media = rs_content.get(mime, {}) or {}
                        fa_sch = fa_media.get("schema") or {}
                        rs_sch = rs_media.get("schema") or {}
                        # Compare $ref or type
                        _mk_test(
                            f"op.requestBody.content[{mime}].schema.$ref[{method.upper()} {path}]",
                            "op.requestBody.content.schema.$ref",
                            fa_sch.get("$ref"), rs_sch.get("$ref"), results,
                        )
                        if budget_exceeded(results, budget):
                            return
                        _mk_test(
                            f"op.requestBody.content[{mime}].schema.type[{method.upper()} {path}]",
                            "op.requestBody.content.schema.type",
                            fa_sch.get("type"), rs_sch.get("type"), results,
                        )
                        if budget_exceeded(results, budget):
                            return

            # responses
            fa_resps = fa_op.get("responses", {}) or {}
            rs_resps = rs_op.get("responses", {}) if isinstance(rs_op, dict) else {}
            rs_resps = rs_resps or {}
            _mk_test(
                f"op.responses.codes[{method.upper()} {path}]",
                "op.responses.codes",
                sorted(fa_resps.keys()), sorted(rs_resps.keys()), results,
            )
            if budget_exceeded(results, budget):
                return
            for code in sorted(fa_resps.keys()):
                fa_r = fa_resps.get(code, {}) or {}
                rs_r = rs_resps.get(code, {}) or {}
                _mk_test(
                    f"op.responses[{code}].description[{method.upper()} {path}]",
                    "op.responses.description",
                    fa_r.get("description"), rs_r.get("description"), results,
                )
                if budget_exceeded(results, budget):
                    return
                fa_rc = fa_r.get("content", {}) or {}
                rs_rc = rs_r.get("content", {}) or {}
                _mk_test(
                    f"op.responses[{code}].mimes[{method.upper()} {path}]",
                    "op.responses.mimes",
                    sorted(fa_rc.keys()), sorted(rs_rc.keys()), results,
                )
                if budget_exceeded(results, budget):
                    return
                for mime in fa_rc.keys():
                    fa_media = fa_rc.get(mime, {}) or {}
                    rs_media = rs_rc.get(mime, {}) or {}
                    fa_sch = fa_media.get("schema") or {}
                    rs_sch = rs_media.get("schema") or {}
                    _mk_test(
                        f"op.responses[{code}].content[{mime}].schema.$ref[{method.upper()} {path}]",
                        "op.responses.content.schema.$ref",
                        fa_sch.get("$ref"), rs_sch.get("$ref"), results,
                    )
                    if budget_exceeded(results, budget):
                        return
                    _mk_test(
                        f"op.responses[{code}].content[{mime}].schema.type[{method.upper()} {path}]",
                        "op.responses.content.schema.type",
                        fa_sch.get("type"), rs_sch.get("type"), results,
                    )
                    if budget_exceeded(results, budget):
                        return

            # security
            fa_sec = fa_op.get("security")
            rs_sec = rs_op.get("security") if isinstance(rs_op, dict) else None
            if fa_sec is not None or rs_sec is not None:
                _mk_test(
                    f"op.security.schemes[{method.upper()} {path}]",
                    "op.security.schemes",
                    fa_sec, rs_sec, results, ordered=False,
                )
                if budget_exceeded(results, budget):
                    return


def gen_component_schema_tests(fa: dict, rs: dict, results: list,
                                budget: Dict[str, int]) -> None:
    fa_sch = fa.get("components", {}).get("schemas", {}) or {}
    rs_sch = rs.get("components", {}).get("schemas", {}) or {}

    # schema existence
    for name in sorted(fa_sch.keys()):
        _mk_test(
            f"components.schemas.exists[{name}]",
            "components.schemas.exists",
            True, name in rs_sch, results,
        )
        if budget_exceeded(results, budget):
            return

    for name in sorted(fa_sch.keys()):
        if name not in rs_sch:
            continue
        fa_s = fa_sch[name] or {}
        rs_s = rs_sch[name] or {}
        for attr in ("title", "type", "description"):
            if attr not in fa_s and attr not in rs_s:
                continue
            _mk_test(
                f"components.schemas[{name}].{attr}",
                f"components.schemas.{attr}",
                fa_s.get(attr), rs_s.get(attr), results,
            )
            if budget_exceeded(results, budget):
                return

        fa_props = fa_s.get("properties", {}) or {}
        rs_props = rs_s.get("properties", {}) or {}
        _mk_test(
            f"components.schemas[{name}].propertyNames",
            "components.schemas.propertyNames",
            sorted(fa_props.keys()), sorted(rs_props.keys()), results,
        )
        if budget_exceeded(results, budget):
            return
        fa_req = fa_s.get("required", []) or []
        rs_req = rs_s.get("required", []) or []
        _mk_test(
            f"components.schemas[{name}].required",
            "components.schemas.required",
            fa_req, rs_req, results, ordered=False,
        )
        if budget_exceeded(results, budget):
            return

        for pname in sorted(fa_props.keys()):
            if pname not in rs_props:
                continue
            fa_p = fa_props[pname] or {}
            rs_p = rs_props[pname] or {}
            for attr in (
                "type", "format", "title", "description", "default", "pattern",
                "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                "minLength", "maxLength",
            ):
                if attr not in fa_p and attr not in rs_p:
                    continue
                _mk_test(
                    f"components.schemas[{name}].properties[{pname}].{attr}",
                    f"components.schemas.properties.{attr}",
                    fa_p.get(attr), rs_p.get(attr), results,
                )
                if budget_exceeded(results, budget):
                    return
            # $ref, enum, anyOf, allOf, oneOf (shape-level)
            for attr in ("$ref",):
                if attr not in fa_p and attr not in rs_p:
                    continue
                _mk_test(
                    f"components.schemas[{name}].properties[{pname}].{attr}",
                    f"components.schemas.properties.{attr}",
                    fa_p.get(attr), rs_p.get(attr), results,
                )
                if budget_exceeded(results, budget):
                    return
            for attr in ("enum",):
                if attr not in fa_p and attr not in rs_p:
                    continue
                _mk_test(
                    f"components.schemas[{name}].properties[{pname}].{attr}",
                    f"components.schemas.properties.{attr}",
                    fa_p.get(attr), rs_p.get(attr), results, ordered=False,
                )
                if budget_exceeded(results, budget):
                    return
            for attr in ("anyOf", "allOf", "oneOf"):
                if attr not in fa_p and attr not in rs_p:
                    continue
                # Compare length and then $refs inside (order-insensitive for anyOf)
                fa_v = fa_p.get(attr, []) or []
                rs_v = rs_p.get(attr, []) or []
                _mk_test(
                    f"components.schemas[{name}].properties[{pname}].{attr}.count",
                    f"components.schemas.properties.{attr}.count",
                    len(fa_v), len(rs_v), results,
                )
                if budget_exceeded(results, budget):
                    return
                # Compare the set of $refs referenced
                fa_refs = sorted(v.get("$ref") for v in fa_v if isinstance(v, dict) and v.get("$ref"))
                rs_refs = sorted(v.get("$ref") for v in rs_v if isinstance(v, dict) and v.get("$ref"))
                _mk_test(
                    f"components.schemas[{name}].properties[{pname}].{attr}.$refs",
                    f"components.schemas.properties.{attr}.$refs",
                    fa_refs, rs_refs, results,
                )
                if budget_exceeded(results, budget):
                    return
                fa_types = sorted(v.get("type") for v in fa_v if isinstance(v, dict) and v.get("type"))
                rs_types = sorted(v.get("type") for v in rs_v if isinstance(v, dict) and v.get("type"))
                _mk_test(
                    f"components.schemas[{name}].properties[{pname}].{attr}.types",
                    f"components.schemas.properties.{attr}.types",
                    fa_types, rs_types, results,
                )
                if budget_exceeded(results, budget):
                    return

        # discriminator
        fa_disc = fa_s.get("discriminator")
        rs_disc = rs_s.get("discriminator")
        if fa_disc is not None or rs_disc is not None:
            _mk_test(
                f"components.schemas[{name}].discriminator",
                "components.schemas.discriminator",
                fa_disc, rs_disc, results,
            )
            if budget_exceeded(results, budget):
                return

        # enum at top-level
        if "enum" in fa_s or "enum" in rs_s:
            _mk_test(
                f"components.schemas[{name}].enum",
                "components.schemas.enum",
                fa_s.get("enum"), rs_s.get("enum"), results, ordered=False,
            )
            if budget_exceeded(results, budget):
                return


def gen_security_scheme_tests(fa: dict, rs: dict, results: list,
                               budget: Dict[str, int]) -> None:
    fa_ss = fa.get("components", {}).get("securitySchemes", {}) or {}
    rs_ss = rs.get("components", {}).get("securitySchemes", {}) or {}
    _mk_test(
        "components.securitySchemes.names",
        "components.securitySchemes.names",
        sorted(fa_ss.keys()), sorted(rs_ss.keys()), results,
    )
    if budget_exceeded(results, budget):
        return
    for name in sorted(fa_ss.keys()):
        fa_s = fa_ss[name] or {}
        rs_s = rs_ss.get(name, {}) or {}
        for attr in ("type", "scheme", "bearerFormat", "name", "in", "description"):
            if attr not in fa_s and attr not in rs_s:
                continue
            _mk_test(
                f"components.securitySchemes[{name}].{attr}",
                f"components.securitySchemes.{attr}",
                fa_s.get(attr), rs_s.get(attr), results,
            )
            if budget_exceeded(results, budget):
                return
        # flows for oauth2
        fa_flows = fa_s.get("flows")
        rs_flows = rs_s.get("flows")
        if fa_flows is not None or rs_flows is not None:
            fa_flows = fa_flows or {}
            rs_flows = rs_flows or {}
            _mk_test(
                f"components.securitySchemes[{name}].flows.types",
                "components.securitySchemes.flows.types",
                sorted(fa_flows.keys()), sorted(rs_flows.keys()), results,
            )
            if budget_exceeded(results, budget):
                return
            for ft in fa_flows.keys():
                fa_flow = fa_flows.get(ft, {}) or {}
                rs_flow = rs_flows.get(ft, {}) or {}
                for fattr in ("tokenUrl", "authorizationUrl", "refreshUrl"):
                    if fattr not in fa_flow and fattr not in rs_flow:
                        continue
                    _mk_test(
                        f"components.securitySchemes[{name}].flows.{ft}.{fattr}",
                        f"components.securitySchemes.flows.{fattr}",
                        fa_flow.get(fattr), rs_flow.get(fattr), results,
                    )
                    if budget_exceeded(results, budget):
                        return
                fa_scopes = fa_flow.get("scopes")
                rs_scopes = rs_flow.get("scopes")
                if fa_scopes is not None or rs_scopes is not None:
                    _mk_test(
                        f"components.securitySchemes[{name}].flows.{ft}.scopes",
                        f"components.securitySchemes.flows.scopes",
                        fa_scopes, rs_scopes, results,
                    )
                    if budget_exceeded(results, budget):
                        return


def gen_top_level_tests(fa: dict, rs: dict, results: list,
                         budget: Dict[str, int]) -> None:
    # openapi version
    _mk_test("openapi.version", "openapi.version",
             fa.get("openapi"), rs.get("openapi"), results)
    if budget_exceeded(results, budget):
        return
    fa_info = fa.get("info", {}) or {}
    rs_info = rs.get("info", {}) or {}
    for k in ("title", "version", "description"):
        if k not in fa_info and k not in rs_info:
            continue
        _mk_test(f"info.{k}", f"info.{k}",
                 fa_info.get(k), rs_info.get(k), results)
        if budget_exceeded(results, budget):
            return
    # components present
    for section in ("paths", "components"):
        _mk_test(f"top.{section}.exists", f"top.{section}.exists",
                 section in fa, section in rs, results)
        if budget_exceeded(results, budget):
            return


def gen_ref_resolution_tests(fa: dict, rs: dict, results: list,
                              budget: Dict[str, int]) -> None:
    """For every $ref we find in the FA schema, make sure RS also resolves."""
    fa_refs = _collect_refs(fa)
    # Sample up to some number to leave budget for others
    sampled = sorted(fa_refs)[:60]
    for ref in sampled:
        fa_target = _resolve_ref(fa, ref)
        rs_target = _resolve_ref(rs, ref)
        _mk_test(
            f"ref.resolves[{ref}]",
            "ref.resolves",
            fa_target is not None, rs_target is not None, results,
        )
        if budget_exceeded(results, budget):
            return


def _collect_refs(obj: Any, out=None) -> set:
    if out is None:
        out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "$ref" and isinstance(v, str):
                out.add(v)
            else:
                _collect_refs(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_refs(v, out)
    return out


def budget_exceeded(results: list, budget: Dict[str, int]) -> bool:
    return len(results) >= budget["max"]


# ── Main ─────────────────────────────────────────────────────────────

def main() -> int:
    print(f"Starting stock FastAPI/uvicorn on :{FA_PORT} ...")
    fa_proc = start_fastapi(FA_PORT)
    print(f"Starting fastapi-rs on :{RS_PORT} ...")
    rs_proc = start_fastapi_rs(RS_PORT)

    try:
        if not wait_for_openapi(FA_PORT):
            stderr = fa_proc.stderr.read().decode(errors="replace") if fa_proc.stderr else ""
            print(f"FATAL: FastAPI did not start on :{FA_PORT}")
            if stderr:
                print("stderr:\n" + stderr[:2000])
            return 1
        if not wait_for_openapi(RS_PORT):
            stderr = rs_proc.stderr.read().decode(errors="replace") if rs_proc.stderr else ""
            print(f"FATAL: fastapi-rs did not start on :{RS_PORT}")
            if stderr:
                print("stderr:\n" + stderr[:2000])
            return 1

        print("Both servers ready. Fetching /openapi.json ...")
        fa_schema = fetch_openapi(FA_PORT)
        rs_schema = fetch_openapi(RS_PORT)
        print(f"  FA: {len(fa_schema.get('paths', {}))} paths, "
              f"{len(fa_schema.get('components', {}).get('schemas', {}))} schemas")
        print(f"  RS: {len(rs_schema.get('paths', {}))} paths, "
              f"{len(rs_schema.get('components', {}).get('schemas', {}))} schemas")

        # Save snapshots for post-mortem
        out_dir = os.path.join(TEST_DIR, "_deep_schema_snapshots")
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "fa_openapi.json"), "w") as f:
            json.dump(fa_schema, f, indent=2, sort_keys=True)
        with open(os.path.join(out_dir, "rs_openapi.json"), "w") as f:
            json.dump(rs_schema, f, indent=2, sort_keys=True)

        # Run test generators in order, each appending into `results`.
        results: List[TestResult] = []
        budget = {"max": MAX_TESTS}

        gen_top_level_tests(fa_schema, rs_schema, results, budget)
        gen_security_scheme_tests(fa_schema, rs_schema, results, budget)
        gen_component_schema_tests(fa_schema, rs_schema, results, budget)
        gen_path_operation_tests(fa_schema, rs_schema, results, budget)
        gen_ref_resolution_tests(fa_schema, rs_schema, results, budget)

        # Cap to MAX_TESTS exactly
        results = results[:MAX_TESTS]

        # Report
        passed = sum(1 for r in results if r.passed)
        failed = sum(1 for r in results if not r.passed)
        total = len(results)

        print("\n" + "=" * 70)
        print(f"DEEP SCHEMA PARITY: {passed}/{total} passed, {failed} failed")
        print("=" * 70)

        if failed:
            # Category aggregation
            cat_fails = Counter(r.category for r in results if not r.passed)
            cat_totals = Counter(r.category for r in results)
            print("\nTop failure categories:")
            for cat, n in cat_fails.most_common(15):
                print(f"  {n:4d} fails / {cat_totals[cat]:4d} total  — {cat}")

            # Print up to first N failing tests
            print("\nFirst 30 failing tests:")
            shown = 0
            for r in results:
                if r.passed:
                    continue
                print(f"  FAIL [{r.category}] {r.name}")
                if r.detail:
                    print(f"         {r.detail}")
                shown += 1
                if shown >= 30:
                    break
        else:
            print("\nAll deep-schema parity checks passed!")

        print("\nSnapshots written to:", out_dir)

        return 0 if failed == 0 else 1
    finally:
        for proc in (fa_proc, rs_proc):
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
