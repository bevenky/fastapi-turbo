#!/usr/bin/env python3
"""Deep validation parity runner for fastapi-turbo.

Starts stock FastAPI on :29600 via subprocess.Popen(uvicorn).
Starts fastapi-turbo on :29601 in a background thread.
For each of ~150 endpoints it sends a crafted INVALID payload and compares
the 422 JSON response from both servers FIELD BY FIELD.

For every invalid case we generate up to ~5 distinct sub-assertions:
  1. status code (both 422)
  2. detail[0].type matches
  3. detail[0].loc matches
  4. detail[0].msg matches (exact, then substring fallback)
  5. detail[0].input matches

Plus multi-error cases that assert ordering + count.

Run:
    cd /Users/venky/tech/fastapi-turbo && source /Users/venky/tech/fastapi_turbo_env/bin/activate \
        && python tests/parity/run_deep_validation_parity.py
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, List, Optional, Tuple

import httpx

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(TEST_DIR))

FA_PORT = 29600
RS_PORT = 29601
FA_URL = f"http://127.0.0.1:{FA_PORT}"
RS_URL = f"http://127.0.0.1:{RS_PORT}"


# ───────────────────────────── Server management ─────────────────────────────

def wait_for_server(url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(url + "/health", timeout=1.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


def start_fastapi() -> subprocess.Popen:
    src = f"""
import os, sys
os.environ['FASTAPI_TURBO_NO_SHIM'] = '1'
sys.path.insert(0, {TEST_DIR!r})
import uvicorn
uvicorn.run('parity_app_deep_validation:app', host='127.0.0.1', port={FA_PORT}, log_level='error')
"""
    return subprocess.Popen(
        [sys.executable, "-c", src],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def start_fastapi_turbo_thread() -> threading.Thread:
    def run():
        # Must install shim before importing the app so stock `from fastapi import …`
        # resolves to fastapi_turbo.
        import fastapi_turbo.compat  # noqa: F401
        fastapi_turbo.compat.install()
        sys.path.insert(0, TEST_DIR)
        import parity_app_deep_validation as papp  # noqa: WPS433
        papp.app.run("127.0.0.1", RS_PORT)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


# ───────────────────────────── Cases ─────────────────────────────

# Each case is:
#   (case_id, method, path, request_kwargs, expected_first_err_loc, description)
# We assert status==422 and compare detail[0].{type,loc,msg,input} between the two.

Case = collections.namedtuple(
    "Case",
    "case_id method path kwargs expected_loc description",
    defaults=(None, None),
)


def all_cases() -> List[Case]:
    cases: List[Case] = []

    def c(cid: str, method: str, path: str, desc: str, **kw):
        cases.append(Case(cid, method, path, kw, None, desc))

    # ─── Query param cases ───
    c("Q001", "get", "/q/int?n=abc", "int_parsing: 'abc' not int")
    c("Q002", "get", "/q/int?n=1.5", "int_parsing: '1.5' not int")
    c("Q003", "get", "/q/int", "missing: required int query param")
    c("Q004", "get", "/q/float?n=abc", "float_parsing: 'abc' not float")
    c("Q005", "get", "/q/bool?flag=zzz", "bool_parsing: 'zzz' not bool")
    c("Q006", "get", "/q/ge?n=-1", "greater_than_equal 0")
    c("Q007", "get", "/q/gt?n=0", "greater_than 0")
    c("Q008", "get", "/q/le?n=200", "less_than_equal 100")
    c("Q009", "get", "/q/lt?n=100", "less_than 100")
    c("Q010", "get", "/q/ge-le?n=-1", "ge=0 violated")
    c("Q011", "get", "/q/ge-le?n=101", "le=100 violated")
    c("Q012", "get", "/q/min-length?s=a", "string_too_short min=3")
    c("Q013", "get", "/q/max-length?s=abcdef", "string_too_long max=5")
    c("Q014", "get", "/q/pattern?code=abc", "string_pattern_mismatch (lower)")
    c("Q015", "get", "/q/pattern?code=AB", "string_pattern_mismatch (short)")
    c("Q016", "get", "/q/enum-str?c=purple", "enum bad value")
    c("Q017", "get", "/q/enum-int?lvl=999", "enum int bad")
    c("Q018", "get", "/q/literal?mode=medium", "literal_error")
    c("Q019", "get", "/q/list-int?ids=1&ids=abc&ids=3", "int_parsing in list idx 1")
    c("Q020", "get", "/q/required", "missing required")
    c("Q021", "get", "/q/uuid?u=not-a-uuid", "uuid_parsing")
    c("Q022", "get", "/q/datetime?d=not-dt", "datetime_from_date_parsing")
    c("Q023", "get", "/q/date?d=not-date", "date_from_datetime_parsing")
    c("Q024", "get", "/q/time?t=not-time", "time_parsing")
    c("Q025", "get", "/q/timedelta?td=not-td", "time_delta_parsing")
    c("Q026", "get", "/q/multiple-of?n=7", "multiple_of=5")
    c("Q027", "get", "/q/decimal?n=abc", "decimal_parsing")

    # ─── Path param cases ───
    c("P001", "get", "/p/int/abc", "path int bad")
    c("P002", "get", "/p/uuid/not-a-uuid", "path uuid bad")
    c("P003", "get", "/p/ge/-1", "path ge=0 violated")
    c("P004", "get", "/p/enum/purple", "path enum bad")

    # ─── Header / cookie cases ───
    c("H001", "get", "/h/int", "missing required header")
    c("H002", "get", "/h/int", "bad int header", headers={"x-count": "abc"})
    c("C001", "get", "/c/int", "missing required cookie")
    c("C002", "get", "/c/int", "bad int cookie", cookies={"session_id": "abc"})

    # ─── Body primitive cases ───
    c("B001", "post", "/b/int", "missing body", json={})
    c("B002", "post", "/b/int", "int_parsing str", json={"n": "abc"})
    c("B003", "post", "/b/int", "int_type on None", json={"n": None})
    c("B004", "post", "/b/int", "int from float", json={"n": 1.5})
    c("B005", "post", "/b/float", "float_parsing", json={"n": "abc"})
    c("B006", "post", "/b/bool", "bool_parsing str", json={"flag": "maybe"})
    c("B007", "post", "/b/bool", "bool on number 2", json={"flag": 2})
    c("B008", "post", "/b/str", "missing str", json={})
    c("B009", "post", "/b/str", "string_type on int", json={"s": 123})

    # ─── Field constraint body cases ───
    c("B010", "post", "/b/ge", "ge=0 violated", json={"n": -1})
    c("B011", "post", "/b/gt", "gt=0 violated", json={"n": 0})
    c("B012", "post", "/b/le", "le=100 violated", json={"n": 200})
    c("B013", "post", "/b/lt", "lt=100 violated", json={"n": 100})
    c("B014", "post", "/b/ge-le", "ge-le low", json={"n": -5})
    c("B015", "post", "/b/ge-le", "ge-le high", json={"n": 200})
    c("B016", "post", "/b/multiple-of", "multiple_of", json={"n": 7})
    c("B017", "post", "/b/min-length", "min_length", json={"s": "a"})
    c("B018", "post", "/b/max-length", "max_length", json={"s": "abcdef"})
    c("B019", "post", "/b/min-max-length", "min_max low", json={"s": "a"})
    c("B020", "post", "/b/min-max-length", "min_max high", json={"s": "abcdef"})
    c("B021", "post", "/b/pattern", "pattern mismatch", json={"code": "abc"})
    c("B022", "post", "/b/decimal-constraint", "decimal constraint", json={"n": "12345.678"})

    # ─── Strict body cases ───
    c("B023", "post", "/b/strict-int", "strict int rejects '1'", json={"n": "1"})
    c("B024", "post", "/b/strict-str", "strict str rejects 1", json={"s": 1})
    c("B025", "post", "/b/strict-bool", "strict bool rejects 'true'", json={"b": "true"})
    c("B026", "post", "/b/strict-bool", "strict bool rejects 1", json={"b": 1})

    # ─── Required / missing ───
    c("B027", "post", "/b/required", "3 missing fields", json={})
    c("B028", "post", "/b/required", "missing b,c", json={"a": 1})
    c("B029", "post", "/b/required", "missing c", json={"a": 1, "b": "s"})

    # ─── Optional / None ───
    c("B030", "post", "/b/optional", "opt with wrong type", json={"a": "x"})

    # ─── Extra fields ───
    c("B031", "post", "/b/forbid-extra", "extra_forbidden", json={"a": 1, "b": 2})

    # ─── Collections ───
    c("B032", "post", "/b/list-int", "list int bad idx 1", json={"xs": [1, "abc", 3]})
    c("B033", "post", "/b/list-int", "list int on non-list", json={"xs": "abc"})
    c("B034", "post", "/b/dict-str-int", "dict str->int bad val", json={"d": {"a": 1, "b": "x"}})
    c("B035", "post", "/b/dict-str-int", "dict on non-dict", json={"d": [1, 2]})
    c("B036", "post", "/b/tuple", "tuple wrong length", json={"t": [1, "s"]})
    c("B037", "post", "/b/tuple", "tuple bad idx 2", json={"t": [1, "s", "x"]})
    c("B038", "post", "/b/nested-list-ints", "nested list bad", json={"rows": [[1, 2], [3, "x"]]})
    c("B039", "post", "/b/list-items", "list model bad nested", json={"items": [{"value": 1}, {"value": "x"}]})

    # ─── Unions / Discriminated ───
    c("B040", "post", "/b/union", "union bad type", json={"x": [1, 2]})
    c("B041", "post", "/b/discriminated", "discr missing kind",
      json={"animal": {"meow_volume": 5}})
    c("B042", "post", "/b/discriminated", "discr wrong kind",
      json={"animal": {"kind": "fish", "bark_loudness": 1}})
    c("B043", "post", "/b/discriminated", "discr right kind missing field",
      json={"animal": {"kind": "cat"}})

    # ─── Enum / Literal body ───
    c("B044", "post", "/b/enum", "enum bad value", json={"c": "purple"})
    c("B045", "post", "/b/literal", "literal wrong value", json={"mode": "medium"})

    # ─── Validators ───
    c("B046", "post", "/b/field-validator", "field_validator raises",
      json={"name": "JOHN"})
    c("B047", "post", "/b/model-validator-before", "mv before raises",
      json={"a": -5, "b": 1})
    c("B048", "post", "/b/model-validator-after", "mv after raises",
      json={"a": 5, "b": 1})

    # ─── Nested ───
    c("B049", "post", "/b/nested", "nested bad zip", json={"name": "n", "address": {"zip": "x", "street": "s"}})
    c("B050", "post", "/b/nested", "nested missing street", json={"name": "n", "address": {"zip": 1}})
    c("B051", "post", "/b/nested", "missing nested entirely", json={"name": "n"})
    c("B052", "post", "/b/nested-list", "nested list user bad",
      json={"users": [{"name": "a", "address": {"zip": 1, "street": "s"}},
                        {"name": "b", "address": {"zip": "bad", "street": "s"}}]})

    # ─── Specialty types ───
    c("B053", "post", "/b/uuid", "uuid bad", json={"u": "not-uuid"})
    c("B054", "post", "/b/uuid4", "uuid4 bad version",
      json={"u": "00000000-0000-0000-0000-000000000000"})
    c("B055", "post", "/b/datetime", "datetime bad", json={"d": "not-dt"})
    c("B056", "post", "/b/date", "date bad", json={"d": "not-date"})
    c("B057", "post", "/b/time", "time bad", json={"t": "not-time"})
    c("B058", "post", "/b/timedelta", "timedelta bad", json={"td": "not-td"})
    c("B059", "post", "/b/httpurl", "httpurl bad", json={"u": "not a url"})
    c("B060", "post", "/b/secret", "secret bad type", json={"pw": 123})
    c("B061", "post", "/b/json", "json wrong type", json={"data": "{\"a\":\"x\"}"})
    c("B062", "post", "/b/json", "json invalid json string", json={"data": "not-json"})

    # ─── Aliases ───
    c("B063", "post", "/b/alias", "alias: use python name",
      json={"item_name": "x"})
    c("B064", "post", "/b/alias", "alias: missing", json={})
    c("B065", "post", "/b/validation-alias", "val alias missing",
      json={"count": 5})
    c("B066", "post", "/b/populate-by-name", "populate-by-name fine",
      json={})  # missing, triggers 422

    # ─── After / Before validators ───
    c("B067", "post", "/b/after-validator", "after validator: odd",
      json={"n": 3})
    c("B068", "post", "/b/before-validator", "before validator: bad after strip",
      json={"name": 123})

    # ─── Multi error ordering ───
    c("B069", "post", "/b/multi-error", "all 3 bad",
      json={"a": "x", "b": "y", "c": "z"})
    c("B070", "post", "/b/multi-error", "2 missing 1 bad",
      json={"a": "x"})

    # ─── Frozen / RootModel ───
    c("B071", "post", "/b/frozen", "frozen missing", json={})
    c("B072", "post", "/b/rootmodel", "root bad idx", json=[1, "x", 3])
    c("B073", "post", "/b/rootmodel", "root not list", json={"a": 1})

    # ─── Recursive / self-ref ───
    c("B074", "post", "/b/tree", "recursive bad nested",
      json={"value": 1, "children": [{"value": "x"}]})
    c("B075", "post", "/b/tree", "recursive deep bad",
      json={"value": 1, "children": [{"value": 2, "children": [{"value": "x"}]}]})

    # ─── Multi body embed ───
    c("B076", "post", "/b/multi-body", "multi-body: missing b",
      json={"a": {"n": 1}})
    c("B077", "post", "/b/multi-body", "multi-body: bad a",
      json={"a": {"n": "x"}, "b": {"s": "ok"}})

    # ─── Dict of models ───
    c("B078", "post", "/b/dict-users", "dict users bad nested",
      json={"users": {"alice": {"name": "a", "address": {"zip": "x", "street": "s"}}}})

    # ─── Optional[int] with required None semantics ───
    c("B079", "post", "/b/opt-none", "opt req missing", json={})
    c("B080", "post", "/b/opt-none", "opt req but str", json={"a": "x"})

    # ─── Bool JSON ───
    c("B081", "post", "/b/bool-json", "bool on 'true' string",
      json={"flag": "true"})  # coerced in JSON mode; expected to PASS
    c("B082", "post", "/b/bool-json", "bool bad",
      json={"flag": "maybe"})

    # ─── Additional edge cases ───
    c("B100", "post", "/b/sci-float", "sci float bad", json={"n": "1e500x"})
    c("B101", "post", "/b/neg-int", "neg int positive", json={"n": 5})
    c("B102", "post", "/b/bytes", "bytes from list", json={"b": [1, 2]})
    c("B103", "post", "/b/set-int", "set with bad el", json={"s": [1, "x", 2]})
    c("B104", "post", "/b/frozenset", "frozenset bad el", json={"s": [1, "x"]})
    c("B105", "post", "/b/complex-string", "complex string short", json={"s": "a"})
    c("B106", "post", "/b/complex-string", "complex string long", json={"s": "abcdefghijkl"})
    c("B107", "post", "/b/complex-string", "complex string bad pattern", json={"s": "ABC"})
    c("B108", "post", "/b/mc-float", "float mc low", json={"n": -0.5})
    c("B109", "post", "/b/mc-float", "float mc high", json={"n": 1.5})
    c("B110", "post", "/b/deep-nested", "deep nested bad leaf",
      json={"a": {"b": {"c": {"value": "x"}}}})
    c("B111", "post", "/b/deep-nested", "deep nested missing mid",
      json={"a": {"b": {"c": {}}}})
    c("B112", "post", "/b/deep-nested", "deep nested top missing",
      json={})
    c("B113", "post", "/b/list-of-dict", "list of dict bad",
      json={"items": [{"a": 1}, {"a": "x"}]})
    c("B114", "post", "/b/dict-of-list", "dict of list bad",
      json={"groups": {"a": [1, 2], "b": [1, "x"]}})
    c("B115", "post", "/b/mixed-constraints", "mixed 4 errors",
      json={"name": "", "age": 999, "score": -5.0, "tags": []})
    c("B116", "post", "/b/mixed-constraints", "mixed: age only",
      json={"name": "x", "age": -1, "score": 50.0, "tags": ["a"]})
    c("B117", "post", "/b/union-three", "union three bad",
      json={"x": [1, 2]})
    c("B118", "post", "/b/list-len", "list too short", json={"xs": [1]})
    c("B119", "post", "/b/list-len", "list too long", json={"xs": [1, 2, 3, 4, 5, 6]})
    c("B120", "post", "/b/forbid-nested", "forbid extra nested",
      json={"inner": {"a": 1, "extra": 2}})
    c("B121", "post", "/b/forbid-nested", "forbid extra outer",
      json={"inner": {"a": 1}, "extra": 2})
    c("B122", "post", "/b/regex-complex", "regex complex bad",
      json={"token": "invalid"})
    c("B123", "post", "/b/nested-opt", "nested opt bad inner",
      json={"child": {"zip": "x", "street": "s"}})
    c("B124", "post", "/b/union-of-models", "union of models both bad",
      json={"entity": {"kind": "cat", "meow_volume": "loud"}})
    c("B125", "post", "/b/union-of-models", "union of models no discr",
      json={"entity": {}})

    # ─── Valid-request parity probes (detail should match = both None) ───
    c("V001", "get", "/q/int?n=42", "valid int")
    c("V002", "get", "/q/ge?n=5", "valid ge")
    c("V003", "post", "/b/int", "valid body int", json={"n": 5})
    c("V004", "post", "/b/ge-le", "valid body ge-le", json={"n": 50})
    c("V005", "post", "/b/list-int", "valid list int", json={"xs": [1, 2, 3]})
    c("V006", "post", "/b/nested", "valid nested",
      json={"name": "n", "address": {"zip": 1, "street": "s"}})
    c("V007", "post", "/b/discriminated", "valid discr",
      json={"animal": {"kind": "cat", "meow_volume": 5}})
    c("V008", "post", "/b/enum", "valid enum", json={"c": "red"})
    c("V009", "post", "/b/literal", "valid literal", json={"mode": "fast"})
    c("V010", "post", "/b/rootmodel", "valid root model", json=[1, 2, 3])

    # ─── Structural: compare plain 422 responses from typo paths (should both 404) ───
    c("N001", "get", "/nope", "unknown path 404")

    return cases


# ───────────────────────────── Comparators ─────────────────────────────

def safe_json(r: httpx.Response):
    try:
        return r.json()
    except Exception:
        return None


def _assert(results: List[dict], case_id: str, prop: str, ok: bool, detail: str = ""):
    results.append({"case": case_id, "prop": prop, "ok": ok, "detail": detail})


def compare_case(case: Case, fa_r: httpx.Response, rs_r: httpx.Response, results: List[dict]):
    cid = case.case_id
    # 1. status code
    _assert(
        results, cid, "status",
        fa_r.status_code == rs_r.status_code,
        f"FA={fa_r.status_code} RS={rs_r.status_code}",
    )

    # Only for invalid cases do we compare detail structure
    if fa_r.status_code != 422:
        # both either 422 or both non-422 is ok for status assertion above.
        # If both are 2xx we also compare bodies loosely.
        if fa_r.status_code < 300 and rs_r.status_code < 300:
            _assert(results, cid, "body-valid",
                    safe_json(fa_r) == safe_json(rs_r),
                    f"FA={_trunc(safe_json(fa_r))} RS={_trunc(safe_json(rs_r))}")
        return

    fa_j = safe_json(fa_r) or {}
    rs_j = safe_json(rs_r) or {}
    fa_det = fa_j.get("detail")
    rs_det = rs_j.get("detail")

    _assert(results, cid, "detail-is-list",
            isinstance(fa_det, list) and isinstance(rs_det, list),
            f"FA={type(fa_det).__name__} RS={type(rs_det).__name__}")

    if not (isinstance(fa_det, list) and isinstance(rs_det, list)):
        return

    # Count parity (number of errors)
    _assert(results, cid, "detail-count",
            len(fa_det) == len(rs_det),
            f"FA={len(fa_det)} RS={len(rs_det)}")

    # Per-error comparisons (iterate over min, common prefix).
    n = min(len(fa_det), len(rs_det))
    for i in range(n):
        fa_e = fa_det[i]
        rs_e = rs_det[i]
        _assert(results, cid, f"err[{i}].type",
                fa_e.get("type") == rs_e.get("type"),
                f"FA={fa_e.get('type')!r} RS={rs_e.get('type')!r}")
        _assert(results, cid, f"err[{i}].loc",
                list(fa_e.get("loc", [])) == list(rs_e.get("loc", [])),
                f"FA={fa_e.get('loc')!r} RS={rs_e.get('loc')!r}")
        _assert(results, cid, f"err[{i}].msg",
                fa_e.get("msg") == rs_e.get("msg"),
                f"FA={_trunc(fa_e.get('msg'))} RS={_trunc(rs_e.get('msg'))}")
        _assert(results, cid, f"err[{i}].input",
                fa_e.get("input") == rs_e.get("input"),
                f"FA={_trunc(fa_e.get('input'))} RS={_trunc(rs_e.get('input'))}")


def _trunc(x, n=80):
    s = repr(x)
    return s if len(s) <= n else s[:n] + "..."


# ───────────────────────────── Main ─────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--filter", "-f", default=None, help="only run cases starting with this prefix")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--fail-dump", default=None, help="write full gap dump to this file")
    args = parser.parse_args()

    print(f"[+] Starting stock FastAPI (uvicorn) on :{FA_PORT}")
    fa_proc = start_fastapi()
    print(f"[+] Starting fastapi-turbo on :{RS_PORT}")
    start_fastapi_turbo_thread()

    try:
        if not wait_for_server(FA_URL):
            err = (fa_proc.stderr.read() or b"").decode()
            print(f"[!] FastAPI failed to boot on :{FA_PORT}\n{err[:500]}")
            return 2
        if not wait_for_server(RS_URL):
            print(f"[!] fastapi-turbo failed to boot on :{RS_PORT}")
            return 2

        print("[+] Both servers ready. Running cases...")
        cases = all_cases()
        if args.filter:
            cases = [c for c in cases if c.case_id.startswith(args.filter.upper())]
        print(f"[+] {len(cases)} cases queued")

        results: List[dict] = []
        client = httpx.Client(timeout=5.0)
        try:
            for case in cases:
                kw = dict(case.kwargs)
                # Cookies and headers need special handling on httpx.Client
                cookies = kw.pop("cookies", None)
                try:
                    fa_r = getattr(client, case.method)(
                        FA_URL + case.path, cookies=cookies, **kw,
                    )
                except Exception as e:
                    _assert(results, case.case_id, "fa-request", False, f"{type(e).__name__}: {e}")
                    continue
                try:
                    rs_r = getattr(client, case.method)(
                        RS_URL + case.path, cookies=cookies, **kw,
                    )
                except Exception as e:
                    _assert(results, case.case_id, "rs-request", False, f"{type(e).__name__}: {e}")
                    continue
                compare_case(case, fa_r, rs_r, results)
        finally:
            client.close()

        passed = sum(1 for r in results if r["ok"])
        failed = sum(1 for r in results if not r["ok"])
        total = len(results)
        print()
        print("=" * 72)
        print(f"Deep validation parity: {passed}/{total} assertions passed ({failed} failed)")
        print("=" * 72)

        if failed:
            # Gap categories — group by (case prefix letter, prop name root)
            by_prop = collections.Counter()
            by_case_prefix = collections.Counter()
            gap_category = collections.Counter()
            for r in results:
                if not r["ok"]:
                    # Keep "err[i].type", "err[i].loc", etc. distinct by stripping index only
                    prop = r["prop"]
                    if prop.startswith("err[") and "]." in prop:
                        # err[0].type -> err[].type
                        prop_root = "err[]" + prop.split("]", 1)[1]
                    else:
                        prop_root = prop
                    by_prop[prop_root] += 1
                    by_case_prefix[r["case"][:1]] += 1

                    # Semantic gap categorization
                    det = r["detail"]
                    if r["prop"] == "status":
                        gap_category["422-expected-but-got-200 or mismatched status"] += 1
                    elif "detail-is-list" in r["prop"]:
                        gap_category["response body not a list (likely 200 vs 422)"] += 1
                    elif r["prop"].endswith(".loc"):
                        if "RS=['query']" in det or "RS=['path']" in det or "RS=['body']" in det:
                            gap_category["loc missing field/index tail (query/path/body root only)"] += 1
                        elif "_" in det and "-" in det:
                            gap_category["loc header name underscore vs hyphen"] += 1
                        else:
                            gap_category["loc mismatch (other)"] += 1
                    elif r["prop"].endswith(".msg"):
                        if "valid list" in det and "valid array" in det:
                            gap_category["msg: 'valid list' vs 'valid array'"] += 1
                        elif "valid dictionary" in det and "object" in det:
                            gap_category["msg: 'dictionary' vs 'object'"] += 1
                        elif "timedelta" in det and "duration" in det:
                            gap_category["msg: 'timedelta' vs 'duration'"] += 1
                        elif "Invalid JSON" in det or "JSON decode" in det:
                            gap_category["msg: JSON parse error wording"] += 1
                        else:
                            gap_category["msg mismatch (other)"] += 1
                    elif r["prop"].endswith(".input"):
                        if "FA='" in det and "RS=" in det:
                            gap_category["input: FA echoes str, RS echoes coerced value"] += 1
                        else:
                            gap_category["input mismatch (other)"] += 1
                    elif r["prop"].endswith(".type"):
                        gap_category["type slug mismatch"] += 1
                    else:
                        gap_category["other"] += 1

            print("\nTop 10 failing assertion kinds:")
            for prop, n in by_prop.most_common(10):
                print(f"  {n:4d}  {prop}")
            print("\nFailures by case prefix (Q=query, P=path, H=header, C=cookie, B=body):")
            for k, n in by_case_prefix.most_common():
                print(f"  {n:4d}  {k}")
            print("\nTop 10 gap categories (semantic grouping):")
            for cat, n in gap_category.most_common(10):
                print(f"  {n:4d}  {cat}")

        # Per-case failure breakdown (aggregated: for each case count # failing props)
        case_fail = collections.defaultdict(list)
        for r in results:
            if not r["ok"]:
                case_fail[r["case"]].append((r["prop"], r["detail"]))

        if args.verbose or args.fail_dump:
            print("\nFailures (per case):")
            for cid in sorted(case_fail):
                print(f"  {cid}: {len(case_fail[cid])} failed")
                for prop, det in case_fail[cid]:
                    print(f"     - {prop}: {det}")

        if args.fail_dump:
            with open(args.fail_dump, "w") as fh:
                json.dump(
                    {
                        "passed": passed,
                        "failed": failed,
                        "total": total,
                        "results": results,
                    },
                    fh,
                    indent=2,
                    default=str,
                )
            print(f"\n[+] Full gap dump written to {args.fail_dump}")

        return 0 if failed == 0 else 1

    finally:
        try:
            fa_proc.send_signal(signal.SIGINT)
            fa_proc.wait(timeout=3)
        except Exception:
            try:
                fa_proc.kill()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
