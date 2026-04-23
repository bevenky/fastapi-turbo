#!/usr/bin/env python3
"""Round 2 deep OpenAPI schema parity runner.

Boots the R2 parity app under BOTH:
  - stock FastAPI on uvicorn (port 29900)
  - fastapi-turbo                  (port 29901)

Then fetches `/openapi.json` from each and runs ~500 DEEP-subtree equality
tests. Each test walks the full subtree and reports EVERY leaf-level
difference — not just the first. This way a single failing test surfaces
many underlying gaps.

On completion, writes `/tmp/r2_gap_report.md` with the top distinct
leaf-path patterns that differ.

Usage:
    cd /Users/venky/tech/fastapi-turbo
    source /Users/venky/tech/fastapi_turbo_env/bin/activate
    python tests/parity/run_deep_schema_parity_r2.py
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import Counter, defaultdict
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

PYTHON = sys.executable
TEST_DIR = os.path.dirname(os.path.abspath(__file__))

FA_PORT = 29900
RS_PORT = 29901

TARGET_TEST_COUNT = 500


# ══════════════════════════════════════════════════════════════════════
# Server management
# ══════════════════════════════════════════════════════════════════════

def start_fastapi(port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [
            PYTHON,
            "-c",
            (
                "import sys, os\n"
                "os.environ['FASTAPI_TURBO_NO_SHIM'] = '1'\n"
                f"sys.path.insert(0, {TEST_DIR!r})\n"
                "import uvicorn\n"
                "from parity_app_deep_schema_r2 import app\n"
                f"uvicorn.run(app, host='127.0.0.1', port={port}, log_level='error')\n"
            ),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def start_fastapi_turbo(port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [
            PYTHON,
            "-c",
            (
                "import sys\n"
                "import fastapi_turbo.compat\n"
                "fastapi_turbo.compat.install()\n"
                f"sys.path.insert(0, {TEST_DIR!r})\n"
                "from parity_app_deep_schema_r2 import app\n"
                f"app.run('127.0.0.1', {port})\n"
            ),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def wait_for_openapi(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/openapi.json", timeout=2.0)
            if r.status_code == 200:
                r.json()
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def fetch_openapi(port: int) -> dict:
    return httpx.get(f"http://127.0.0.1:{port}/openapi.json", timeout=10.0).json()


def kill(proc: Optional[subprocess.Popen]) -> None:
    if proc is None:
        return
    try:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            pass
        proc.kill()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════
# Deep compare
# ══════════════════════════════════════════════════════════════════════

_MAX_DIFFS_PER_NODE = 2000   # cap per test to avoid pathological output
_REF_RE = re.compile(r"^#/")


def _resolve_ref(doc: dict, ref: str) -> Any:
    if not isinstance(ref, str) or not _REF_RE.match(ref):
        return {"__UNRESOLVABLE_REF__": ref}
    cur: Any = doc
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return {"__UNRESOLVABLE_REF__": ref}
    return cur


def _inline_refs(node: Any, doc: dict, seen: Optional[set] = None, depth: int = 0) -> Any:
    """Recursively inline `$ref` targets from `doc`.

    Cycle-safe: if a ref is re-encountered on the current chain, we emit a
    marker ("__CYCLE__", ref) so two documents with isomorphic cycles still
    deep-compare equal.
    """
    if seen is None:
        seen = set()
    if depth > 40:
        return ("__MAX_DEPTH__",)
    if isinstance(node, dict):
        if "$ref" in node and isinstance(node["$ref"], str):
            ref = node["$ref"]
            if ref in seen:
                return {"__CYCLE__": ref}
            seen2 = seen | {ref}
            target = _resolve_ref(doc, ref)
            inlined = _inline_refs(target, doc, seen2, depth + 1)
            # Merge sibling keys of the $ref (OpenAPI allows these adjacent to $ref).
            siblings = {k: v for k, v in node.items() if k != "$ref"}
            if siblings and isinstance(inlined, dict):
                merged = dict(inlined)
                for k, v in siblings.items():
                    merged[k] = _inline_refs(v, doc, seen2, depth + 1)
                return merged
            return inlined
        return {k: _inline_refs(v, doc, seen, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline_refs(x, doc, seen, depth + 1) for x in node]
    return node


class Diff:
    __slots__ = ("path", "kind", "fa", "rs")

    def __init__(self, path: str, kind: str, fa: Any, rs: Any):
        self.path = path
        self.kind = kind
        self.fa = fa
        self.rs = rs

    def short(self, limit: int = 100) -> str:
        def s(v: Any) -> str:
            try:
                t = json.dumps(v, sort_keys=True, default=str)
            except Exception:
                t = repr(v)
            return (t[:limit] + "...") if len(t) > limit else t
        return f"[{self.kind}] {self.path}  FA={s(self.fa)}  RS={s(self.rs)}"

    # pattern used for "leaf-path family" aggregation in the gap report
    def pattern(self) -> str:
        # Replace numeric indices [N] with [*]
        p = re.sub(r"\[\d+\]", "[*]", self.path)
        # Replace quoted path segments with '*' to group schemas together
        p = re.sub(r"\{[^}]+\}", "{*}", p)
        return f"{self.kind}:{p}"


def deep_compare(
    fa_node: Any,
    rs_node: Any,
    path: str = "",
    diffs: Optional[List[Diff]] = None,
) -> List[Diff]:
    """Walk both subtrees in parallel and collect EVERY leaf difference.

    Never stops at first difference. Returns full list of diffs.
    """
    if diffs is None:
        diffs = []
    if len(diffs) >= _MAX_DIFFS_PER_NODE:
        return diffs

    # Type mismatch
    if type(fa_node) is not type(rs_node) and not (
        isinstance(fa_node, (int, float)) and isinstance(rs_node, (int, float))
    ):
        diffs.append(Diff(path or "$", "type", _describe_type(fa_node), _describe_type(rs_node)))
        return diffs

    if isinstance(fa_node, dict):
        fa_keys = list(fa_node.keys())
        rs_keys = list(rs_node.keys())
        fa_set = set(fa_keys)
        rs_set = set(rs_keys)
        for k in fa_keys:
            if k not in rs_set:
                diffs.append(Diff(f"{path}.{k}", "missing_in_rs", fa_node[k], None))
        for k in rs_keys:
            if k not in fa_set:
                diffs.append(Diff(f"{path}.{k}", "extra_in_rs", None, rs_node[k]))
        # Key-order check (only where it matters: `properties`, `required`)
        if path.endswith(".properties") or path.endswith("properties"):
            if fa_keys != rs_keys and fa_set == rs_set:
                diffs.append(Diff(f"{path}[key-order]", "key_order", fa_keys, rs_keys))
        for k in fa_keys:
            if k in rs_set:
                deep_compare(fa_node[k], rs_node[k], f"{path}.{k}", diffs)
                if len(diffs) >= _MAX_DIFFS_PER_NODE:
                    return diffs
        return diffs

    if isinstance(fa_node, list):
        if len(fa_node) != len(rs_node):
            diffs.append(Diff(f"{path}[len]", "list_len", len(fa_node), len(rs_node)))
        for i, (a, b) in enumerate(zip(fa_node, rs_node)):
            deep_compare(a, b, f"{path}[{i}]", diffs)
            if len(diffs) >= _MAX_DIFFS_PER_NODE:
                return diffs
        return diffs

    # Leaf
    if fa_node != rs_node:
        # numeric close-enough? Keep strict equality.
        diffs.append(Diff(path or "$", "value", fa_node, rs_node))
    return diffs


def _describe_type(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    return type(v).__name__


# ══════════════════════════════════════════════════════════════════════
# Test harness
# ══════════════════════════════════════════════════════════════════════

class TestResult:
    __slots__ = ("name", "category", "passed", "diffs", "error")

    def __init__(
        self,
        name: str,
        category: str,
        passed: bool,
        diffs: Optional[List[Diff]] = None,
        error: Optional[str] = None,
    ):
        self.name = name
        self.category = category
        self.passed = passed
        self.diffs = diffs or []
        self.error = error


def _safe(fn: Callable[[], List[Diff]]) -> Tuple[bool, List[Diff], Optional[str]]:
    try:
        diffs = fn()
        return (len(diffs) == 0, diffs, None)
    except Exception as e:  # pragma: no cover
        return (False, [], f"exception: {e!r}")


# ══════════════════════════════════════════════════════════════════════
# Test generation
# ══════════════════════════════════════════════════════════════════════

def _subpath(doc: dict, *keys: Any) -> Any:
    cur: Any = doc
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return None
    return cur


def build_tests(fa: dict, rs: dict) -> List[Tuple[str, str, Callable[[], List[Diff]]]]:
    """Build ~500 deep tests. Each test returns a list of leaf diffs.

    Each test is a FULL SUBTREE deep equality. A single test collects every
    leaf-level difference in its subtree — so ~500 deep tests surface
    thousands of individual diffs when things drift.
    """
    tests: List[Tuple[str, str, Callable[[], List[Diff]]]] = []

    # ── Top-level metadata (4 tests) ────────────────────────────────
    for key in ("openapi", "info", "servers", "tags"):
        tests.append((
            f"toplevel::{key}",
            "toplevel",
            (lambda k=key: deep_compare(fa.get(k), rs.get(k), f"${k}")),
        ))

    # ── Per-path × per-method DEEP subtree tests (4 per op) ─────────
    # 1) FULL operation subtree with all $refs inlined — catches EVERYTHING
    # 2) requestBody subtree only — isolates body/form/multipart drift
    # 3) responses subtree only — isolates response schema drift
    # 4) parameters list — isolates query/path/header/cookie drift
    fa_paths = fa.get("paths", {}) or {}
    rs_paths = rs.get("paths", {}) or {}

    path_set = set(fa_paths.keys()) | set(rs_paths.keys())
    for path in sorted(path_set):
        fa_p = fa_paths.get(path) or {}
        rs_p = rs_paths.get(path) or {}
        methods = (set(fa_p.keys()) | set(rs_p.keys())) - {
            "parameters",
            "summary",
            "description",
            "servers",
        }
        for m in sorted(methods):
            tests.append((
                f"op::{path}::{m}::full",
                "op_full",
                (lambda p=path, m=m: deep_compare(
                    _inline_refs(_subpath(fa, "paths", p, m), fa),
                    _inline_refs(_subpath(rs, "paths", p, m), rs),
                    f"$paths[{p}][{m}]",
                )),
            ))
            tests.append((
                f"op::{path}::{m}::requestBody",
                "op_requestBody",
                (lambda p=path, m=m: deep_compare(
                    _inline_refs(_subpath(fa, "paths", p, m, "requestBody"), fa),
                    _inline_refs(_subpath(rs, "paths", p, m, "requestBody"), rs),
                    f"$paths[{p}][{m}].requestBody",
                )),
            ))
            tests.append((
                f"op::{path}::{m}::responses",
                "op_responses",
                (lambda p=path, m=m: deep_compare(
                    _inline_refs(_subpath(fa, "paths", p, m, "responses") or {}, fa),
                    _inline_refs(_subpath(rs, "paths", p, m, "responses") or {}, rs),
                    f"$paths[{p}][{m}].responses",
                )),
            ))
            tests.append((
                f"op::{path}::{m}::parameters",
                "op_parameters",
                (lambda p=path, m=m: deep_compare(
                    _inline_refs(_subpath(fa, "paths", p, m, "parameters") or [], fa),
                    _inline_refs(_subpath(rs, "paths", p, m, "parameters") or [], rs),
                    f"$paths[{p}][{m}].parameters",
                )),
            ))

    # ── Schemas: each one, FULL deep subtree (1 per schema) ─────────
    fa_schemas = (fa.get("components", {}) or {}).get("schemas", {}) or {}
    rs_schemas = (rs.get("components", {}) or {}).get("schemas", {}) or {}

    tests.append((
        "components::schemas::keys",
        "components_schema_keys",
        (lambda: deep_compare(
            sorted(fa_schemas.keys()), sorted(rs_schemas.keys()),
            "$components.schemas[keys]",
        )),
    ))

    all_schema_names = sorted(set(fa_schemas.keys()) | set(rs_schemas.keys()))
    for name in all_schema_names:
        fa_s = fa_schemas.get(name)
        rs_s = rs_schemas.get(name)
        tests.append((
            f"schema::{name}::full",
            "schema_full",
            (lambda n=name, a=fa_s, b=rs_s: deep_compare(
                _inline_refs(a, fa) if a is not None else None,
                _inline_refs(b, rs) if b is not None else None,
                f"$schemas[{n}]",
            )),
        ))

    # ── Security schemes (1 per scheme) ─────────────────────────────
    fa_sec = (fa.get("components", {}) or {}).get("securitySchemes", {}) or {}
    rs_sec = (rs.get("components", {}) or {}).get("securitySchemes", {}) or {}
    tests.append((
        "components::securitySchemes::keys",
        "components_security_keys",
        (lambda: deep_compare(
            sorted(fa_sec.keys()), sorted(rs_sec.keys()),
            "$components.securitySchemes[keys]",
        )),
    ))
    for name in sorted(set(fa_sec.keys()) | set(rs_sec.keys())):
        tests.append((
            f"securityScheme::{name}::full",
            "securityScheme_full",
            (lambda n=name: deep_compare(
                fa_sec.get(n), rs_sec.get(n),
                f"$components.securitySchemes[{n}]",
            )),
        ))

    # ── components sub-containers (1 per container) ─────────────────
    for subkey in (
        "parameters",
        "requestBodies",
        "responses",
        "headers",
        "examples",
        "links",
        "callbacks",
    ):
        tests.append((
            f"components::{subkey}",
            f"components_{subkey}",
            (lambda sk=subkey: deep_compare(
                (fa.get("components") or {}).get(sk),
                (rs.get("components") or {}).get(sk),
                f"$components.{sk}",
            )),
        ))

    # ── Tags deep (1 per tag) ───────────────────────────────────────
    fa_tags = fa.get("tags") or []
    rs_tags = rs.get("tags") or []
    fa_by_name = {t.get("name"): t for t in fa_tags if isinstance(t, dict)}
    rs_by_name = {t.get("name"): t for t in rs_tags if isinstance(t, dict)}
    for name in sorted(set(fa_by_name.keys()) | set(rs_by_name.keys())):
        tests.append((
            f"tag::{name}::full",
            "tag_full",
            (lambda n=name: deep_compare(
                fa_by_name.get(n), rs_by_name.get(n), f"$tags[{n}]"
            )),
        ))

    # ── Per-schema property deep subtrees (1 per property) ─────────
    # Each test walks the full subtree of ONE property (with $refs inlined).
    # This surfaces drift in field-level keywords: format, examples, default,
    # title, description, enum, const, pattern, minimum/maximum, exclusive*,
    # multipleOf, minLength/maxLength, minItems/maxItems, uniqueItems,
    # readOnly/writeOnly, deprecated, nullable, anyOf/oneOf, items, etc.
    for name in all_schema_names:
        fa_s = fa_schemas.get(name) or {}
        rs_s = rs_schemas.get(name) or {}
        props_union = set((fa_s.get("properties") or {}).keys()) | set(
            (rs_s.get("properties") or {}).keys()
        )
        for prop in sorted(props_union):
            tests.append((
                f"schema::{name}::prop::{prop}",
                "schema_property",
                (lambda n=name, a=fa_s, b=rs_s, p=prop: deep_compare(
                    _inline_refs((a.get("properties") or {}).get(p), fa),
                    _inline_refs((b.get("properties") or {}).get(p), rs),
                    f"$schemas[{n}].properties[{p}]",
                )),
            ))

    # ── Schema-level required/type/discriminator/allOf/anyOf ───────
    # 1 subtree test per (schema, structural-key) where one or both sides
    # have a value. Surfaces allOf/oneOf/discriminator/required drift.
    for name in all_schema_names:
        fa_s = fa_schemas.get(name) or {}
        rs_s = rs_schemas.get(name) or {}
        for meta in (
            "required",
            "discriminator",
            "allOf",
            "oneOf",
            "anyOf",
            "additionalProperties",
        ):
            if (meta in fa_s) or (meta in rs_s):
                tests.append((
                    f"schema::{name}::{meta}",
                    f"schema_{meta}",
                    (lambda n=name, a=fa_s, b=rs_s, m=meta: deep_compare(
                        _inline_refs(a.get(m), fa),
                        _inline_refs(b.get(m), rs),
                        f"$schemas[{n}].{m}",
                    )),
                ))

    # ── Per-operation per-status response (deep subtree) ────────────
    # One test per (path, method, status). Each compares the full response
    # object — description, headers, content[*].schema, examples, links.
    for path in sorted(path_set):
        fa_p = fa_paths.get(path) or {}
        rs_p = rs_paths.get(path) or {}
        methods = (set(fa_p.keys()) | set(rs_p.keys())) - {
            "parameters",
            "summary",
            "description",
            "servers",
        }
        for m in sorted(methods):
            fa_resp = _subpath(fa, "paths", path, m, "responses") or {}
            rs_resp = _subpath(rs, "paths", path, m, "responses") or {}
            status_set = set(fa_resp.keys()) | set(rs_resp.keys())
            for st in sorted(status_set):
                tests.append((
                    f"op::{path}::{m}::responses::{st}",
                    "op_response_status",
                    (lambda p=path, m=m, st=st: deep_compare(
                        _inline_refs(_subpath(fa, "paths", p, m, "responses", st), fa),
                        _inline_refs(_subpath(rs, "paths", p, m, "responses", st), rs),
                        f"$paths[{p}][{m}].responses[{st}]",
                    )),
                ))

    # ── Sanity: full $paths and full $components.schemas subtrees ──
    # These two dwarf all others — a single catch-all that ensures
    # nothing sneaks through.
    tests.append((
        "toplevel::paths::full",
        "toplevel_paths_full",
        (lambda: deep_compare(
            _inline_refs(fa.get("paths"), fa),
            _inline_refs(rs.get("paths"), rs),
            "$paths",
        )),
    ))
    tests.append((
        "toplevel::components.schemas::full",
        "toplevel_schemas_full",
        (lambda: deep_compare(
            _inline_refs((fa.get("components") or {}).get("schemas"), fa),
            _inline_refs((rs.get("components") or {}).get("schemas"), rs),
            "$components.schemas",
        )),
    ))

    return tests


# ══════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════

def main() -> int:
    print("Starting uvicorn (stock FastAPI) on", FA_PORT)
    fa_proc = start_fastapi(FA_PORT)
    print("Starting fastapi-turbo on", RS_PORT)
    rs_proc = start_fastapi_turbo(RS_PORT)

    try:
        if not wait_for_openapi(FA_PORT):
            stderr = ""
            try:
                if fa_proc.stderr is not None:
                    stderr = fa_proc.stderr.read(4096).decode(errors="replace")
            except Exception:
                pass
            print("FastAPI server did not come up:", stderr, file=sys.stderr)
            return 2
        if not wait_for_openapi(RS_PORT):
            stderr = ""
            try:
                if rs_proc.stderr is not None:
                    stderr = rs_proc.stderr.read(4096).decode(errors="replace")
            except Exception:
                pass
            print("fastapi-turbo server did not come up:", stderr, file=sys.stderr)
            return 2

        fa = fetch_openapi(FA_PORT)
        rs = fetch_openapi(RS_PORT)
    finally:
        # We can kill servers now — everything we need is in-memory
        kill(fa_proc)
        kill(rs_proc)

    tests = build_tests(fa, rs)
    print(f"Generated {len(tests)} deep tests")

    # If we have far more than 500, we still run ALL — the task says ~500, and
    # limiting would hide gaps. But mention the count in the report.
    results: List[TestResult] = []
    for name, cat, fn in tests:
        ok, diffs, err = _safe(fn)
        results.append(TestResult(name, cat, ok and err is None, diffs, err))

    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed

    # Per-category summary
    cat_stats: Dict[str, Dict[str, int]] = defaultdict(lambda: {"pass": 0, "fail": 0})
    for r in results:
        cat_stats[r.category]["pass" if r.passed else "fail"] += 1

    # Aggregate leaf diffs
    total_leaf_diffs = 0
    pattern_counter: Counter = Counter()
    kind_counter: Counter = Counter()
    sample_by_pattern: Dict[str, Diff] = {}
    for r in results:
        if r.passed:
            continue
        for d in r.diffs:
            total_leaf_diffs += 1
            pat = d.pattern()
            pattern_counter[pat] += 1
            kind_counter[d.kind] += 1
            sample_by_pattern.setdefault(pat, d)

    # ── Output ─────────────────────────────────────────────────────
    print()
    print("=" * 72)
    print(f"R2 DEEP SCHEMA PARITY RESULTS")
    print("=" * 72)
    print(f"Total tests:            {len(results)}")
    print(f"Passed:                 {passed}")
    print(f"Failed:                 {failed}")
    print(f"Total leaf differences: {total_leaf_diffs}")
    print()

    print("By category:")
    for cat in sorted(cat_stats.keys()):
        s = cat_stats[cat]
        tot = s["pass"] + s["fail"]
        print(f"  {cat:35s}  pass={s['pass']:4d}  fail={s['fail']:4d}  total={tot}")
    print()

    print("Diff kinds:")
    for k, c in kind_counter.most_common():
        print(f"  {k:20s}  {c}")
    print()

    print("Top 20 distinct leaf-path patterns that differ:")
    for pat, cnt in pattern_counter.most_common(20):
        sample = sample_by_pattern[pat]
        print(f"  [{cnt:4d}]  {pat}")
        print(f"           e.g. {sample.short(180)}")
    print()

    # First 25 failing tests — show full diff list for each (capped)
    failing = [r for r in results if not r.passed]
    if failing:
        print(f"Showing full diffs for first 25 of {len(failing)} failing tests:")
        print("-" * 72)
        for r in failing[:25]:
            print(f"FAIL  {r.category:25s}  {r.name}")
            if r.error:
                print(f"      ERROR: {r.error}")
            shown = 0
            for d in r.diffs:
                if shown >= 10:
                    print(f"      ... ({len(r.diffs) - 10} more diffs in this test)")
                    break
                print(f"      - {d.short(200)}")
                shown += 1
            print()

    # ── Gap report ─────────────────────────────────────────────────
    report_path = "/tmp/r2_gap_report.md"
    with open(report_path, "w") as f:
        f.write("# R2 Deep Schema Parity — Gap Report\n\n")
        f.write(f"- Total tests: **{len(results)}**\n")
        f.write(f"- Passed: **{passed}**\n")
        f.write(f"- Failed: **{failed}**\n")
        f.write(f"- Total leaf-level differences: **{total_leaf_diffs}**\n\n")

        f.write("## Results by category\n\n")
        f.write("| Category | Passed | Failed | Total |\n")
        f.write("|---|---:|---:|---:|\n")
        for cat in sorted(cat_stats.keys()):
            s = cat_stats[cat]
            tot = s["pass"] + s["fail"]
            f.write(f"| {cat} | {s['pass']} | {s['fail']} | {tot} |\n")

        f.write("\n## Diff kinds\n\n")
        f.write("| Kind | Count |\n|---|---:|\n")
        for k, c in kind_counter.most_common():
            f.write(f"| {k} | {c} |\n")

        f.write("\n## Top 40 distinct leaf-path patterns that differ\n\n")
        f.write("| Count | Pattern | Sample |\n|---:|---|---|\n")
        for pat, cnt in pattern_counter.most_common(40):
            sample = sample_by_pattern[pat]
            s = sample.short(160).replace("|", "\\|")
            f.write(f"| {cnt} | `{pat}` | {s} |\n")

        f.write("\n## First 100 failing tests\n\n")
        for r in failing[:100]:
            f.write(f"### FAIL  `{r.name}`\n\n")
            f.write(f"- category: `{r.category}`\n")
            if r.error:
                f.write(f"- ERROR: `{r.error}`\n")
            f.write(f"- leaf diffs: **{len(r.diffs)}**\n\n")
            if r.diffs:
                for d in r.diffs[:20]:
                    line = d.short(200).replace("|", "\\|")
                    f.write(f"  - {line}\n")
                if len(r.diffs) > 20:
                    f.write(f"  - ... and {len(r.diffs) - 20} more\n")
            f.write("\n")

    print(f"Gap report written to {report_path}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
