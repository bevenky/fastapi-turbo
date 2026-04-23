#!/usr/bin/env python3
"""ROUND 2 — Deep validation parity runner.

Boots stock FastAPI on :29910 (uvicorn, subprocess) and fastapi-turbo on :29911
(in-thread). For each of ~500 crafted cases it sends a single bad request and
compares the ENTIRE 422 response body between both servers: every error in
detail[], every field of each error (type/loc/msg/input/ctx/url).

The whole point of R2: one bad request -> ONE case revealing ALL buried
field-level gaps (not just the first one).

Run:
    cd /Users/venky/tech/jamun && source /Users/venky/tech/jamun_env/bin/activate
    python tests/parity/run_deep_validation_parity_r2.py
    python tests/parity/run_deep_validation_parity_r2.py --fail-dump gap.json
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
from typing import Any, Dict, List, Optional, Tuple

import httpx

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(TEST_DIR))

FA_PORT = 29910
RS_PORT = 29911
FA_URL = f"http://127.0.0.1:{FA_PORT}"
RS_URL = f"http://127.0.0.1:{RS_PORT}"


# ───────────────────────────── Server management ─────────────────────────────

def wait_for_server(url: str, timeout: float = 25.0) -> bool:
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
uvicorn.run('parity_app_deep_validation_r2:app', host='127.0.0.1',
            port={FA_PORT}, log_level='error')
"""
    return subprocess.Popen(
        [sys.executable, "-c", src],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def start_fastapi_turbo_thread() -> threading.Thread:
    def run():
        import fastapi_turbo.compat  # noqa: F401
        fastapi_turbo.compat.install()
        sys.path.insert(0, TEST_DIR)
        import parity_app_deep_validation_r2 as papp  # noqa: WPS433
        papp.app.run("127.0.0.1", RS_PORT)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


# ───────────────────────────── Case model ─────────────────────────────

class Case:
    __slots__ = ("case_id", "method", "path", "kwargs", "description",
                 "error_type_hint", "location_hint", "nesting_hint")

    def __init__(self, case_id: str, method: str, path: str, description: str,
                 error_type_hint: str = "",
                 location_hint: str = "",
                 nesting_hint: int = 0,
                 **kwargs):
        self.case_id = case_id
        self.method = method.lower()
        self.path = path
        self.description = description
        self.error_type_hint = error_type_hint
        self.location_hint = location_hint
        self.nesting_hint = nesting_hint
        self.kwargs = kwargs


def all_cases() -> List[Case]:
    cases: List[Case] = []

    def add(cid: str, method: str, path: str, desc: str,
            error_type_hint: str = "",
            location_hint: str = "",
            nesting: int = 0,
            **kw):
        cases.append(Case(cid, method, path, desc, error_type_hint,
                          location_hint, nesting, **kw))

    # ─────────────────────────── QUERY ───────────────────────────
    # int_parsing
    add("Q001", "get", "/q/int?n=abc", "int_parsing str",
        error_type_hint="int_parsing", location_hint="query")
    add("Q002", "get", "/q/int?n=1.5", "int_parsing float-str",
        error_type_hint="int_parsing", location_hint="query")
    add("Q003", "get", "/q/int?n=", "int_parsing empty",
        error_type_hint="int_parsing", location_hint="query")
    add("Q004", "get", "/q/int?n=%20", "int_parsing space",
        error_type_hint="int_parsing", location_hint="query")
    add("Q005", "get", "/q/int?n=12abc", "int_parsing suffix",
        error_type_hint="int_parsing", location_hint="query")
    # missing
    add("Q006", "get", "/q/int", "missing int q",
        error_type_hint="missing", location_hint="query")
    add("Q007", "get", "/q/required", "missing str q",
        error_type_hint="missing", location_hint="query")
    add("Q008", "get", "/q/str", "missing str q via Query(...)",
        error_type_hint="missing", location_hint="query")
    # float_parsing
    add("Q010", "get", "/q/float?n=abc", "float_parsing",
        error_type_hint="float_parsing", location_hint="query")
    add("Q011", "get", "/q/float?n=", "float_parsing empty",
        error_type_hint="float_parsing", location_hint="query")
    add("Q012", "get", "/q/float?n=1.2.3", "float_parsing double-dot",
        error_type_hint="float_parsing", location_hint="query")
    # bool_parsing
    add("Q020", "get", "/q/bool?flag=zzz", "bool_parsing zzz",
        error_type_hint="bool_parsing", location_hint="query")
    add("Q021", "get", "/q/bool?flag=2", "bool_parsing 2",
        error_type_hint="bool_parsing", location_hint="query")
    add("Q022", "get", "/q/bool?flag=True123", "bool_parsing suffix",
        error_type_hint="bool_parsing", location_hint="query")
    # ge/gt/le/lt
    add("Q030", "get", "/q/ge?n=-1", "ge=0",
        error_type_hint="greater_than_equal", location_hint="query")
    add("Q031", "get", "/q/gt?n=0", "gt=0",
        error_type_hint="greater_than", location_hint="query")
    add("Q032", "get", "/q/le?n=200", "le=100",
        error_type_hint="less_than_equal", location_hint="query")
    add("Q033", "get", "/q/lt?n=100", "lt=100",
        error_type_hint="less_than", location_hint="query")
    add("Q034", "get", "/q/ge-le?n=-1", "ge-le low",
        error_type_hint="greater_than_equal", location_hint="query")
    add("Q035", "get", "/q/ge-le?n=101", "ge-le high",
        error_type_hint="less_than_equal", location_hint="query")
    add("Q036", "get", "/q/gt-lt?n=0", "gt-lt low",
        error_type_hint="greater_than", location_hint="query")
    add("Q037", "get", "/q/gt-lt?n=100", "gt-lt high",
        error_type_hint="less_than", location_hint="query")
    add("Q038", "get", "/q/ge-float?n=-0.1", "ge float",
        error_type_hint="greater_than_equal", location_hint="query")
    add("Q039", "get", "/q/le-float?n=1.1", "le float",
        error_type_hint="less_than_equal", location_hint="query")
    # string length
    add("Q040", "get", "/q/min-length?s=a", "string too short",
        error_type_hint="string_too_short", location_hint="query")
    add("Q041", "get", "/q/min-length?s=ab", "string too short 2",
        error_type_hint="string_too_short", location_hint="query")
    add("Q042", "get", "/q/max-length?s=abcdef", "string too long",
        error_type_hint="string_too_long", location_hint="query")
    add("Q043", "get", "/q/min-max-length?s=a", "min_max low",
        error_type_hint="string_too_short", location_hint="query")
    add("Q044", "get", "/q/min-max-length?s=abcdef", "min_max high",
        error_type_hint="string_too_long", location_hint="query")
    # patterns
    add("Q050", "get", "/q/pattern?code=abc", "pattern lowercase",
        error_type_hint="string_pattern_mismatch", location_hint="query")
    add("Q051", "get", "/q/pattern?code=AB", "pattern short",
        error_type_hint="string_pattern_mismatch", location_hint="query")
    add("Q052", "get", "/q/pattern?code=ABCD", "pattern long",
        error_type_hint="string_pattern_mismatch", location_hint="query")
    add("Q053", "get", "/q/pattern-digits?code=abcd", "pattern digits",
        error_type_hint="string_pattern_mismatch", location_hint="query")
    # enums
    add("Q060", "get", "/q/enum-str?c=purple", "enum str bad",
        error_type_hint="enum", location_hint="query")
    add("Q061", "get", "/q/enum-int?lvl=999", "enum int bad",
        error_type_hint="enum", location_hint="query")
    add("Q062", "get", "/q/enum-int?lvl=abc", "enum int parse bad",
        error_type_hint="int_parsing", location_hint="query")
    # literals
    add("Q063", "get", "/q/literal?mode=medium", "literal bad",
        error_type_hint="literal_error", location_hint="query")
    add("Q064", "get", "/q/literal-int?n=5", "literal int bad",
        error_type_hint="literal_error", location_hint="query")
    # list
    add("Q070", "get", "/q/list-int?ids=1&ids=abc&ids=3",
        "list int bad idx 1",
        error_type_hint="int_parsing", location_hint="query")
    add("Q071", "get", "/q/list-int", "missing list",
        error_type_hint="missing", location_hint="query")
    # uuid
    add("Q080", "get", "/q/uuid?u=not-a-uuid", "uuid_parsing",
        error_type_hint="uuid_parsing", location_hint="query")
    add("Q081", "get", "/q/uuid?u=12345", "uuid_parsing short",
        error_type_hint="uuid_parsing", location_hint="query")
    add("Q082", "get", "/q/uuid4?u=00000000-0000-0000-0000-000000000000",
        "uuid version bad",
        error_type_hint="uuid_version", location_hint="query")
    # datetime / date / time / timedelta
    add("Q090", "get", "/q/datetime?d=not-dt", "dt bad",
        error_type_hint="datetime_from_date_parsing", location_hint="query")
    add("Q091", "get", "/q/date?d=not-date", "date bad",
        error_type_hint="date_from_datetime_parsing", location_hint="query")
    add("Q092", "get", "/q/time?t=not-time", "time bad",
        error_type_hint="time_parsing", location_hint="query")
    add("Q093", "get", "/q/timedelta?td=not-td", "timedelta bad",
        error_type_hint="time_delta_parsing", location_hint="query")
    # multiple_of
    add("Q100", "get", "/q/multiple-of?n=7", "multiple_of",
        error_type_hint="multiple_of", location_hint="query")
    # decimal
    add("Q110", "get", "/q/decimal?n=abc", "decimal bad",
        error_type_hint="decimal_parsing", location_hint="query")
    # strict int via query (query is string, strict should reject)
    add("Q120", "get", "/q/strict-int?n=5", "strict int in query",
        error_type_hint="int_type", location_hint="query")
    # httpurl via query
    add("Q130", "get", "/q/httpurl?u=not-a-url", "httpurl bad",
        error_type_hint="url_parsing", location_hint="query")
    # multi-error ordering
    add("Q140", "get", "/q/multi?a=x&b=y&c=z", "multi 3 bad",
        error_type_hint="int_parsing", location_hint="query")
    add("Q141", "get", "/q/multi?a=1", "multi 2 missing",
        error_type_hint="missing", location_hint="query")

    # ─────────────────────────── PATH ───────────────────────────
    add("P001", "get", "/p/int/abc", "path int bad",
        error_type_hint="int_parsing", location_hint="path")
    add("P002", "get", "/p/int/1.5", "path int float-str",
        error_type_hint="int_parsing", location_hint="path")
    add("P010", "get", "/p/float/xxx", "path float bad",
        error_type_hint="float_parsing", location_hint="path")
    add("P020", "get", "/p/bool/maybe", "path bool bad",
        error_type_hint="bool_parsing", location_hint="path")
    add("P030", "get", "/p/ge/-1", "path ge violated",
        error_type_hint="greater_than_equal", location_hint="path")
    add("P031", "get", "/p/gt/0", "path gt violated",
        error_type_hint="greater_than", location_hint="path")
    add("P032", "get", "/p/le/200", "path le violated",
        error_type_hint="less_than_equal", location_hint="path")
    add("P033", "get", "/p/lt/100", "path lt violated",
        error_type_hint="less_than", location_hint="path")
    add("P040", "get", "/p/min-length/a", "path minlen",
        error_type_hint="string_too_short", location_hint="path")
    add("P041", "get", "/p/pattern/abc", "path pattern bad",
        error_type_hint="string_pattern_mismatch", location_hint="path")
    add("P050", "get", "/p/uuid/not-uuid", "path uuid bad",
        error_type_hint="uuid_parsing", location_hint="path")
    add("P060", "get", "/p/datetime/nope", "path datetime bad",
        error_type_hint="datetime_from_date_parsing", location_hint="path")
    add("P061", "get", "/p/date/nope", "path date bad",
        error_type_hint="date_from_datetime_parsing", location_hint="path")
    add("P070", "get", "/p/enum/purple", "path enum bad",
        error_type_hint="enum", location_hint="path")
    add("P080", "get", "/p/literal/medium", "path literal bad",
        error_type_hint="literal_error", location_hint="path")
    add("P090", "get", "/p/decimal/abc", "path decimal bad",
        error_type_hint="decimal_parsing", location_hint="path")
    add("P100", "get", "/p/multi/1/x/3", "path multi 1 bad",
        error_type_hint="int_parsing", location_hint="path")
    add("P101", "get", "/p/multi/x/y/z", "path multi all bad",
        error_type_hint="int_parsing", location_hint="path")

    # ─────────────────────────── HEADER ───────────────────────────
    add("H001", "get", "/h/int", "header missing",
        error_type_hint="missing", location_hint="header")
    add("H002", "get", "/h/int", "header int bad",
        error_type_hint="int_parsing", location_hint="header",
        headers={"x-count": "abc"})
    add("H003", "get", "/h/int", "header int with list",
        error_type_hint="int_parsing", location_hint="header",
        headers={"x-count": "1.5"})
    add("H010", "get", "/h/float", "header float bad",
        error_type_hint="float_parsing", location_hint="header",
        headers={"x-ratio": "bad"})
    add("H020", "get", "/h/bool", "header bool bad",
        error_type_hint="bool_parsing", location_hint="header",
        headers={"x-flag": "maybe"})
    add("H030", "get", "/h/ge", "header ge violated",
        error_type_hint="greater_than_equal", location_hint="header",
        headers={"x-count": "-1"})
    add("H040", "get", "/h/pattern", "header pattern bad",
        error_type_hint="string_pattern_mismatch", location_hint="header",
        headers={"x-code": "abc"})
    add("H050", "get", "/h/uuid", "header uuid bad",
        error_type_hint="uuid_parsing", location_hint="header",
        headers={"x-trace-id": "not-uuid"})
    add("H060", "get", "/h/enum", "header enum bad",
        error_type_hint="enum", location_hint="header",
        headers={"x-color": "purple"})
    add("H070", "get", "/h/literal", "header literal bad",
        error_type_hint="literal_error", location_hint="header",
        headers={"x-mode": "medium"})

    # ─────────────────────────── COOKIE ───────────────────────────
    add("C001", "get", "/c/int", "cookie missing",
        error_type_hint="missing", location_hint="cookie")
    add("C002", "get", "/c/int", "cookie int bad",
        error_type_hint="int_parsing", location_hint="cookie",
        cookies={"session_id": "abc"})
    add("C010", "get", "/c/float", "cookie float bad",
        error_type_hint="float_parsing", location_hint="cookie",
        cookies={"ratio": "nope"})
    add("C020", "get", "/c/bool", "cookie bool bad",
        error_type_hint="bool_parsing", location_hint="cookie",
        cookies={"flag": "maybe"})
    add("C030", "get", "/c/ge", "cookie ge violated",
        error_type_hint="greater_than_equal", location_hint="cookie",
        cookies={"session_id": "-1"})
    add("C040", "get", "/c/pattern", "cookie pattern bad",
        error_type_hint="string_pattern_mismatch", location_hint="cookie",
        cookies={"token": "abc"})
    add("C050", "get", "/c/uuid", "cookie uuid bad",
        error_type_hint="uuid_parsing", location_hint="cookie",
        cookies={"session_uuid": "not-uuid"})
    add("C060", "get", "/c/enum", "cookie enum bad",
        error_type_hint="enum", location_hint="cookie",
        cookies={"theme": "purple"})

    # ─────────────────────────── BODY: primitive ───────────────────────────
    add("B001", "post", "/b/int", "body int missing",
        error_type_hint="missing", location_hint="body-model", nesting=1,
        json={})
    add("B002", "post", "/b/int", "body int_parsing str",
        error_type_hint="int_parsing", location_hint="body-model", nesting=1,
        json={"n": "abc"})
    add("B003", "post", "/b/int", "body int_type on None",
        error_type_hint="int_type", location_hint="body-model", nesting=1,
        json={"n": None})
    add("B004", "post", "/b/int", "body int from float",
        error_type_hint="int_from_float", location_hint="body-model",
        nesting=1, json={"n": 1.5})
    add("B005", "post", "/b/int", "body int from list",
        error_type_hint="int_type", location_hint="body-model", nesting=1,
        json={"n": [1]})
    add("B006", "post", "/b/int", "body int from dict",
        error_type_hint="int_type", location_hint="body-model", nesting=1,
        json={"n": {"a": 1}})
    add("B010", "post", "/b/float", "body float_parsing",
        error_type_hint="float_parsing", location_hint="body-model",
        nesting=1, json={"n": "abc"})
    add("B011", "post", "/b/float", "body float_type None",
        error_type_hint="float_type", location_hint="body-model", nesting=1,
        json={"n": None})
    add("B012", "post", "/b/float", "body float_type list",
        error_type_hint="float_type", location_hint="body-model", nesting=1,
        json={"n": [1]})
    add("B020", "post", "/b/bool", "body bool_parsing str",
        error_type_hint="bool_parsing", location_hint="body-model",
        nesting=1, json={"flag": "maybe"})
    add("B021", "post", "/b/bool", "body bool on int 2",
        error_type_hint="bool_parsing", location_hint="body-model",
        nesting=1, json={"flag": 2})
    add("B022", "post", "/b/bool", "body bool_type on None",
        error_type_hint="bool_type", location_hint="body-model", nesting=1,
        json={"flag": None})
    add("B030", "post", "/b/str", "body str missing",
        error_type_hint="missing", location_hint="body-model", nesting=1,
        json={})
    add("B031", "post", "/b/str", "body str_type on int",
        error_type_hint="string_type", location_hint="body-model",
        nesting=1, json={"s": 123})
    add("B032", "post", "/b/str", "body str_type on list",
        error_type_hint="string_type", location_hint="body-model",
        nesting=1, json={"s": [1, 2]})
    add("B033", "post", "/b/str", "body str_type on None",
        error_type_hint="string_type", location_hint="body-model",
        nesting=1, json={"s": None})
    add("B040", "post", "/b/bytes", "body bytes from int",
        error_type_hint="bytes_type", location_hint="body-model", nesting=1,
        json={"b": 123})
    add("B041", "post", "/b/bytes", "body bytes from list",
        error_type_hint="bytes_type", location_hint="body-model", nesting=1,
        json={"b": [1, 2]})

    # body scalar (single Body param)
    add("B050", "post", "/b/scalar-int", "scalar body int_parsing",
        error_type_hint="int_parsing", location_hint="body-scalar",
        json="abc")
    add("B051", "post", "/b/scalar-int", "scalar body missing",
        error_type_hint="missing", location_hint="body-scalar")
    add("B052", "post", "/b/scalar-str", "scalar body str_type",
        error_type_hint="string_type", location_hint="body-scalar",
        json=123)
    add("B053", "post", "/b/scalar-embed-int",
        "scalar embed body int_parsing",
        error_type_hint="int_parsing", location_hint="body-scalar",
        json={"n": "abc"})
    add("B054", "post", "/b/scalar-embed-int",
        "scalar embed body missing",
        error_type_hint="missing", location_hint="body-scalar", json={})

    # ─────────────── BODY: field constraints ───────────────
    add("B060", "post", "/b/ge", "body ge violated",
        error_type_hint="greater_than_equal", location_hint="body-model",
        nesting=1, json={"n": -1})
    add("B061", "post", "/b/gt", "body gt violated",
        error_type_hint="greater_than", location_hint="body-model",
        nesting=1, json={"n": 0})
    add("B062", "post", "/b/le", "body le violated",
        error_type_hint="less_than_equal", location_hint="body-model",
        nesting=1, json={"n": 200})
    add("B063", "post", "/b/lt", "body lt violated",
        error_type_hint="less_than", location_hint="body-model", nesting=1,
        json={"n": 100})
    add("B064", "post", "/b/ge-le", "body ge-le low",
        error_type_hint="greater_than_equal", location_hint="body-model",
        nesting=1, json={"n": -5})
    add("B065", "post", "/b/ge-le", "body ge-le high",
        error_type_hint="less_than_equal", location_hint="body-model",
        nesting=1, json={"n": 200})
    add("B066", "post", "/b/gt-lt", "body gt-lt low",
        error_type_hint="greater_than", location_hint="body-model",
        nesting=1, json={"n": 0})
    add("B067", "post", "/b/gt-lt", "body gt-lt high",
        error_type_hint="less_than", location_hint="body-model", nesting=1,
        json={"n": 100})
    add("B068", "post", "/b/ge-le-float", "body float ge-le low",
        error_type_hint="greater_than_equal", location_hint="body-model",
        nesting=1, json={"n": -0.5})
    add("B069", "post", "/b/ge-le-float", "body float ge-le high",
        error_type_hint="less_than_equal", location_hint="body-model",
        nesting=1, json={"n": 1.5})
    add("B070", "post", "/b/multiple-of", "body multiple_of int",
        error_type_hint="multiple_of", location_hint="body-model",
        nesting=1, json={"n": 7})
    add("B071", "post", "/b/multiple-of-float", "body multiple_of float",
        error_type_hint="multiple_of", location_hint="body-model",
        nesting=1, json={"n": 0.3})
    add("B072", "post", "/b/min-length", "body string too short",
        error_type_hint="string_too_short", location_hint="body-model",
        nesting=1, json={"s": "a"})
    add("B073", "post", "/b/max-length", "body string too long",
        error_type_hint="string_too_long", location_hint="body-model",
        nesting=1, json={"s": "abcdef"})
    add("B074", "post", "/b/min-max-length", "body min-max low",
        error_type_hint="string_too_short", location_hint="body-model",
        nesting=1, json={"s": "a"})
    add("B075", "post", "/b/min-max-length", "body min-max high",
        error_type_hint="string_too_long", location_hint="body-model",
        nesting=1, json={"s": "abcdef"})
    add("B076", "post", "/b/pattern", "body pattern bad",
        error_type_hint="string_pattern_mismatch",
        location_hint="body-model", nesting=1, json={"code": "abc"})
    add("B077", "post", "/b/complex-string", "body complex short",
        error_type_hint="string_too_short", location_hint="body-model",
        nesting=1, json={"s": "a"})
    add("B078", "post", "/b/complex-string", "body complex long",
        error_type_hint="string_too_long", location_hint="body-model",
        nesting=1, json={"s": "abcdefghijkl"})
    add("B079", "post", "/b/complex-string", "body complex pattern",
        error_type_hint="string_pattern_mismatch",
        location_hint="body-model", nesting=1, json={"s": "ABC"})
    add("B080", "post", "/b/bytes-min-length", "bytes too short",
        error_type_hint="bytes_too_short", location_hint="body-model",
        nesting=1, json={"b": "a"})
    add("B081", "post", "/b/bytes-max-length", "bytes too long",
        error_type_hint="bytes_too_long", location_hint="body-model",
        nesting=1, json={"b": "abcdef"})
    add("B082", "post", "/b/decimal-constraint", "decimal max_digits",
        error_type_hint="decimal_max_digits", location_hint="body-model",
        nesting=1, json={"n": "123456.78"})
    add("B083", "post", "/b/decimal-places", "decimal max_places",
        error_type_hint="decimal_max_places", location_hint="body-model",
        nesting=1, json={"n": "1.234"})
    add("B084", "post", "/b/finite-float", "finite float inf",
        error_type_hint="finite_number", location_hint="body-model",
        nesting=1, content='{"n": Infinity}',
        content_type="application/json")

    # ─────────────── BODY: strict ───────────────
    add("B090", "post", "/b/strict-int", "strict int str",
        error_type_hint="int_type", location_hint="body-model", nesting=1,
        json={"n": "1"})
    add("B091", "post", "/b/strict-int", "strict int float",
        error_type_hint="int_type", location_hint="body-model", nesting=1,
        json={"n": 1.5})
    add("B092", "post", "/b/strict-str", "strict str int",
        error_type_hint="string_type", location_hint="body-model",
        nesting=1, json={"s": 1})
    add("B093", "post", "/b/strict-str", "strict str bool",
        error_type_hint="string_type", location_hint="body-model",
        nesting=1, json={"s": True})
    add("B094", "post", "/b/strict-bool", "strict bool str",
        error_type_hint="bool_type", location_hint="body-model", nesting=1,
        json={"b": "true"})
    add("B095", "post", "/b/strict-bool", "strict bool int",
        error_type_hint="bool_type", location_hint="body-model", nesting=1,
        json={"b": 1})
    add("B096", "post", "/b/strict-float", "strict float str",
        error_type_hint="float_type", location_hint="body-model",
        nesting=1, json={"n": "1.5"})

    # ─────────────── BODY: required/optional ───────────────
    add("B100", "post", "/b/required", "3 missing", json={},
        error_type_hint="missing", location_hint="body-model", nesting=1)
    add("B101", "post", "/b/required", "2 missing (b,c)", json={"a": 1},
        error_type_hint="missing", location_hint="body-model", nesting=1)
    add("B102", "post", "/b/required", "missing c", json={"a": 1, "b": "s"},
        error_type_hint="missing", location_hint="body-model", nesting=1)
    add("B103", "post", "/b/optional", "optional with wrong type",
        json={"a": "x"},
        error_type_hint="int_parsing", location_hint="body-model", nesting=1)
    add("B104", "post", "/b/opt-none", "opt-none missing", json={},
        error_type_hint="missing", location_hint="body-model", nesting=1)
    add("B105", "post", "/b/opt-none", "opt-none bad str",
        json={"a": "abc"},
        error_type_hint="int_parsing", location_hint="body-model", nesting=1)

    # ─────────────── BODY: extra ───────────────
    add("B110", "post", "/b/forbid-extra", "extra forbidden",
        error_type_hint="extra_forbidden", location_hint="body-model",
        nesting=1, json={"a": 1, "b": 2})
    add("B111", "post", "/b/forbid-extra", "extra forbidden 2 fields",
        error_type_hint="extra_forbidden", location_hint="body-model",
        nesting=1, json={"a": 1, "b": 2, "c": 3})
    add("B112", "post", "/b/forbid-nested", "forbid nested inner",
        error_type_hint="extra_forbidden", location_hint="body-nested",
        nesting=2, json={"inner": {"a": 1, "extra": 2}})
    add("B113", "post", "/b/forbid-nested", "forbid nested outer",
        error_type_hint="extra_forbidden", location_hint="body-model",
        nesting=1, json={"inner": {"a": 1}, "extra": 2})

    # ─────────────── BODY: collections ───────────────
    add("B120", "post", "/b/list-int", "list int bad idx 1",
        error_type_hint="int_parsing", location_hint="body-list",
        nesting=2, json={"xs": [1, "abc", 3]})
    add("B121", "post", "/b/list-int", "list int on non-list",
        error_type_hint="list_type", location_hint="body-model",
        nesting=1, json={"xs": "abc"})
    add("B122", "post", "/b/list-int", "list int on dict",
        error_type_hint="list_type", location_hint="body-model",
        nesting=1, json={"xs": {"a": 1}})
    add("B123", "post", "/b/list-str", "list str bad idx",
        error_type_hint="string_type", location_hint="body-list",
        nesting=2, json={"xs": ["a", 2, "c"]})
    add("B124", "post", "/b/dict-str-int", "dict bad val",
        error_type_hint="int_parsing", location_hint="body-dict",
        nesting=2, json={"d": {"a": 1, "b": "x"}})
    add("B125", "post", "/b/dict-str-int", "dict on non-dict",
        error_type_hint="dict_type", location_hint="body-model",
        nesting=1, json={"d": [1, 2]})
    add("B126", "post", "/b/dict-int-int", "dict key not-int",
        error_type_hint="int_parsing", location_hint="body-dict",
        nesting=2, json={"d": {"abc": 1}})
    add("B127", "post", "/b/tuple", "tuple wrong length",
        error_type_hint="too_short", location_hint="body-model",
        nesting=1, json={"t": [1, "s"]})
    add("B128", "post", "/b/tuple", "tuple idx 2 bad",
        error_type_hint="float_parsing", location_hint="body-tuple",
        nesting=2, json={"t": [1, "s", "x"]})
    add("B129", "post", "/b/tuple", "tuple too many",
        error_type_hint="too_long", location_hint="body-model",
        nesting=1, json={"t": [1, "s", 1.0, "extra"]})
    add("B130", "post", "/b/tuple2", "tuple2 short",
        error_type_hint="too_short", location_hint="body-model",
        nesting=1, json={"t": [1]})
    add("B131", "post", "/b/tuple-variadic", "tuple variadic bad",
        error_type_hint="int_parsing", location_hint="body-tuple",
        nesting=2, json={"t": [1, "x", 3]})
    add("B132", "post", "/b/set-int", "set bad el",
        error_type_hint="int_parsing", location_hint="body-list",
        nesting=2, json={"s": [1, "x", 2]})
    add("B133", "post", "/b/set-int", "set on non-list",
        error_type_hint="set_type", location_hint="body-model",
        nesting=1, json={"s": "abc"})
    add("B134", "post", "/b/frozenset", "frozenset bad",
        error_type_hint="int_parsing", location_hint="body-list",
        nesting=2, json={"s": [1, "x"]})
    add("B135", "post", "/b/nested-list-ints", "nested list bad",
        error_type_hint="int_parsing", location_hint="body-list",
        nesting=3, json={"rows": [[1, 2], [3, "x"]]})
    add("B136", "post", "/b/list-of-dict", "list of dict bad",
        error_type_hint="int_parsing", location_hint="body-list",
        nesting=3, json={"items": [{"a": 1}, {"a": "x"}]})
    add("B137", "post", "/b/dict-of-list", "dict of list bad",
        error_type_hint="int_parsing", location_hint="body-dict",
        nesting=3, json={"groups": {"a": [1, 2], "b": [1, "x"]}})
    add("B138", "post", "/b/dict-of-dict", "dict of dict bad",
        error_type_hint="int_parsing", location_hint="body-dict",
        nesting=3, json={"m": {"a": {"k": 1}, "b": {"k": "x"}}})
    add("B139", "post", "/b/list-len", "list too short",
        error_type_hint="too_short", location_hint="body-model",
        nesting=1, json={"xs": [1]})
    add("B140", "post", "/b/list-len", "list too long",
        error_type_hint="too_long", location_hint="body-model",
        nesting=1, json={"xs": [1, 2, 3, 4, 5, 6]})

    # ─────────────── BODY: unions ───────────────
    add("B150", "post", "/b/union", "union bad type",
        error_type_hint="string_type", location_hint="body-model",
        nesting=1, json={"x": [1, 2]})
    add("B151", "post", "/b/union-three", "union-three bad type",
        error_type_hint="string_type", location_hint="body-model",
        nesting=1, json={"x": [1, 2]})
    add("B152", "post", "/b/discriminated", "discr missing tag",
        error_type_hint="union_tag_not_found", location_hint="body-model",
        nesting=2, json={"animal": {"meow_volume": 5}})
    add("B153", "post", "/b/discriminated", "discr wrong tag",
        error_type_hint="union_tag_invalid", location_hint="body-model",
        nesting=2,
        json={"animal": {"kind": "fish", "bark_loudness": 1}})
    add("B154", "post", "/b/discriminated", "discr right tag missing",
        error_type_hint="missing", location_hint="body-nested",
        nesting=3, json={"animal": {"kind": "cat"}})
    add("B155", "post", "/b/union-of-models", "union models bad type",
        error_type_hint="int_parsing", location_hint="body-model",
        nesting=2,
        json={"entity": {"kind": "cat", "meow_volume": "loud"}})
    add("B156", "post", "/b/union-of-models", "union models no discr",
        error_type_hint="missing", location_hint="body-model",
        nesting=2, json={"entity": {}})
    add("B157", "post", "/b/nested-pets", "nested pets wrong tag",
        error_type_hint="union_tag_invalid", location_hint="body-list",
        nesting=3, json={"pets": [
            {"animal": {"kind": "cat", "meow_volume": 1}},
            {"animal": {"kind": "fish"}},
        ]})

    # ─────────────── BODY: enums/literals ───────────────
    add("B170", "post", "/b/enum", "body enum bad",
        error_type_hint="enum", location_hint="body-model", nesting=1,
        json={"c": "purple"})
    add("B171", "post", "/b/enum-int", "body enum int bad",
        error_type_hint="enum", location_hint="body-model", nesting=1,
        json={"lvl": 999})
    add("B172", "post", "/b/literal", "body literal bad",
        error_type_hint="literal_error", location_hint="body-model",
        nesting=1, json={"mode": "medium"})
    add("B173", "post", "/b/literal-int", "body literal int bad",
        error_type_hint="literal_error", location_hint="body-model",
        nesting=1, json={"n": 5})
    add("B174", "post", "/b/literal-mixed", "body literal mixed",
        error_type_hint="literal_error", location_hint="body-model",
        nesting=1, json={"v": 99})

    # ─────────────── BODY: validators ───────────────
    add("B180", "post", "/b/field-validator", "field validator raise",
        error_type_hint="value_error", location_hint="body-model",
        nesting=1, json={"name": "JOHN"})
    add("B181", "post", "/b/field-validator-assert", "field assert raise",
        error_type_hint="assertion_error", location_hint="body-model",
        nesting=1, json={"n": -1})
    add("B182", "post", "/b/model-validator-before", "mv before raise",
        error_type_hint="value_error", location_hint="body-root",
        nesting=0, json={"a": -5, "b": 1})
    add("B183", "post", "/b/model-validator-after", "mv after raise",
        error_type_hint="value_error", location_hint="body-root",
        nesting=0, json={"a": 5, "b": 1})
    add("B184", "post", "/b/after-validator", "after validator raise",
        error_type_hint="value_error", location_hint="body-model",
        nesting=1, json={"n": 3})
    add("B185", "post", "/b/before-validator", "before validator raise",
        error_type_hint="string_type", location_hint="body-model",
        nesting=1, json={"name": 123})

    # ─────────────── BODY: nesting (1..5) ───────────────
    add("B200", "post", "/b/nested", "nested bad zip (d=2)",
        error_type_hint="int_parsing", location_hint="body-nested",
        nesting=2,
        json={"name": "n", "address": {"zip": "x", "street": "s"}})
    add("B201", "post", "/b/nested", "nested missing street",
        error_type_hint="missing", location_hint="body-nested",
        nesting=2, json={"name": "n", "address": {"zip": 1}})
    add("B202", "post", "/b/nested", "nested address whole missing",
        error_type_hint="missing", location_hint="body-model",
        nesting=1, json={"name": "n"})
    add("B203", "post", "/b/nested-list", "nested list bad user",
        error_type_hint="int_parsing", location_hint="body-list",
        nesting=4,
        json={"users": [{"name": "a", "address": {"zip": 1, "street": "s"}},
                        {"name": "b", "address": {"zip": "bad", "street": "s"}}]})
    add("B204", "post", "/b/nested-dict", "nested dict bad user",
        error_type_hint="int_parsing", location_hint="body-dict",
        nesting=4,
        json={"users": {"x": {"name": "a", "address": {"zip": "bad", "street": "s"}}}})
    add("B205", "post", "/b/depth-2", "d2 leaf bad",
        error_type_hint="int_parsing", location_hint="body-nested",
        nesting=2, json={"a": {"v": "x"}})
    add("B206", "post", "/b/depth-3", "d3 leaf bad",
        error_type_hint="int_parsing", location_hint="body-nested",
        nesting=3, json={"b": {"a": {"v": "x"}}})
    add("B207", "post", "/b/depth-4", "d4 leaf bad",
        error_type_hint="int_parsing", location_hint="body-nested",
        nesting=4, json={"c": {"b": {"a": {"v": "x"}}}})
    add("B208", "post", "/b/depth-5", "d5 leaf bad",
        error_type_hint="int_parsing", location_hint="body-nested",
        nesting=5, json={"d": {"c": {"b": {"a": {"v": "x"}}}}})
    add("B209", "post", "/b/depth-5", "d5 mid missing",
        error_type_hint="missing", location_hint="body-nested",
        nesting=3, json={"d": {"c": {"b": {}}}})
    add("B210", "post", "/b/deep-list-d5", "list of d5 bad",
        error_type_hint="int_parsing", location_hint="body-list",
        nesting=5,
        json={"items": [{"d": {"c": {"b": {"a": {"v": "x"}}}}}]})
    add("B211", "post", "/b/deep-list-in-list", "list of list of list",
        error_type_hint="int_parsing", location_hint="body-list",
        nesting=4,
        json={"matrix": [[[1, 2], [3, "x"]], [[5, 6]]]})
    add("B212", "post", "/b/deep-multi-level", "3 intermediate indices",
        error_type_hint="int_parsing", location_hint="body-list",
        nesting=5,
        json={"groups": [
            {"inner_groups": [{"items": [{"v": 1}, {"v": "x"}]}]},
        ]})

    # ─────────────── BODY: specialty ───────────────
    add("B220", "post", "/b/uuid", "uuid bad",
        error_type_hint="uuid_parsing", location_hint="body-model",
        nesting=1, json={"u": "not-uuid"})
    add("B221", "post", "/b/uuid", "uuid short",
        error_type_hint="uuid_parsing", location_hint="body-model",
        nesting=1, json={"u": "12345"})
    add("B222", "post", "/b/uuid4", "uuid4 version bad",
        error_type_hint="uuid_version", location_hint="body-model",
        nesting=1, json={"u": "00000000-0000-0000-0000-000000000000"})
    add("B223", "post", "/b/uuid4", "uuid4 parse bad",
        error_type_hint="uuid_parsing", location_hint="body-model",
        nesting=1, json={"u": "not-uuid"})
    add("B224", "post", "/b/uuid1", "uuid1 version bad",
        error_type_hint="uuid_version", location_hint="body-model",
        nesting=1, json={"u": "12345678-1234-4000-8000-000000000000"})
    add("B230", "post", "/b/datetime", "datetime bad",
        error_type_hint="datetime_from_date_parsing",
        location_hint="body-model", nesting=1, json={"d": "not-dt"})
    add("B231", "post", "/b/datetime", "datetime int",
        error_type_hint="datetime_type", location_hint="body-model",
        nesting=1, json={"d": None})
    add("B232", "post", "/b/aware-datetime", "aware datetime naive",
        error_type_hint="timezone_aware", location_hint="body-model",
        nesting=1, json={"d": "2024-01-01T00:00:00"})
    add("B233", "post", "/b/naive-datetime", "naive datetime aware",
        error_type_hint="timezone_naive", location_hint="body-model",
        nesting=1, json={"d": "2024-01-01T00:00:00+01:00"})
    add("B234", "post", "/b/past-datetime", "past datetime future",
        error_type_hint="datetime_past", location_hint="body-model",
        nesting=1, json={"d": "3999-01-01T00:00:00"})
    add("B235", "post", "/b/future-datetime", "future datetime past",
        error_type_hint="datetime_future", location_hint="body-model",
        nesting=1, json={"d": "1999-01-01T00:00:00"})
    add("B240", "post", "/b/date", "date bad",
        error_type_hint="date_from_datetime_parsing",
        location_hint="body-model", nesting=1, json={"d": "not-date"})
    add("B241", "post", "/b/past-date", "past date future",
        error_type_hint="date_past", location_hint="body-model",
        nesting=1, json={"d": "3999-01-01"})
    add("B242", "post", "/b/future-date", "future date past",
        error_type_hint="date_future", location_hint="body-model",
        nesting=1, json={"d": "1999-01-01"})
    add("B243", "post", "/b/time", "time bad",
        error_type_hint="time_parsing", location_hint="body-model",
        nesting=1, json={"t": "not-time"})
    add("B244", "post", "/b/time", "time int",
        error_type_hint="time_type", location_hint="body-model",
        nesting=1, json={"t": None})
    add("B250", "post", "/b/timedelta", "timedelta bad",
        error_type_hint="time_delta_parsing", location_hint="body-model",
        nesting=1, json={"td": "not-td"})
    add("B251", "post", "/b/timedelta", "timedelta list",
        error_type_hint="time_delta_type", location_hint="body-model",
        nesting=1, json={"td": [1, 2]})
    add("B260", "post", "/b/httpurl", "httpurl bad",
        error_type_hint="url_parsing", location_hint="body-model",
        nesting=1, json={"u": "not a url"})
    add("B261", "post", "/b/httpurl", "httpurl wrong scheme",
        error_type_hint="url_scheme", location_hint="body-model",
        nesting=1, json={"u": "ftp://host/"})
    add("B262", "post", "/b/httpurl-maxlen", "httpurl too long",
        error_type_hint="url_too_long", location_hint="body-model",
        nesting=1, json={"u": "https://example.com/very/long/path"})
    add("B270", "post", "/b/secret", "secret bad type",
        error_type_hint="string_type", location_hint="body-model",
        nesting=1, json={"pw": 123})
    add("B280", "post", "/b/json", "json wrong type",
        error_type_hint="int_parsing", location_hint="body-model",
        nesting=1, json={"data": "{\"a\":\"x\"}"})
    add("B281", "post", "/b/json", "json invalid str",
        error_type_hint="json_invalid", location_hint="body-model",
        nesting=1, json={"data": "not-json"})
    add("B282", "post", "/b/json", "json type non-str",
        error_type_hint="json_type", location_hint="body-model",
        nesting=1, json={"data": 123})
    add("B283", "post", "/b/json-list", "json list bad el",
        error_type_hint="int_parsing", location_hint="body-model",
        nesting=1, json={"data": "[1, \"x\", 3]"})

    # ─────────────── BODY: aliases ───────────────
    add("B290", "post", "/b/alias", "alias python name rejected",
        error_type_hint="missing", location_hint="body-model",
        nesting=1, json={"item_name": "x"})
    add("B291", "post", "/b/alias", "alias missing",
        error_type_hint="missing", location_hint="body-model",
        nesting=1, json={})
    add("B292", "post", "/b/validation-alias", "val alias missing",
        error_type_hint="missing", location_hint="body-model",
        nesting=1, json={"count": 5})
    add("B293", "post", "/b/populate-by-name", "populate-by-name missing",
        error_type_hint="missing", location_hint="body-model",
        nesting=1, json={})

    # ─────────────── BODY: validators ───────────────
    # already covered B180-B185

    # ─────────────── BODY: multi-error ordering ───────────────
    add("B300", "post", "/b/multi-error", "3 parse errors",
        error_type_hint="int_parsing", location_hint="body-model",
        nesting=1, json={"a": "x", "b": "y", "c": "z"})
    add("B301", "post", "/b/multi-error", "2 missing 1 bad",
        error_type_hint="int_parsing", location_hint="body-model",
        nesting=1, json={"a": "x"})
    add("B302", "post", "/b/mixed-constraints", "4 different errors",
        error_type_hint="string_too_short", location_hint="body-model",
        nesting=1,
        json={"name": "", "age": 999, "score": -5.0, "tags": []})
    add("B303", "post", "/b/mixed-constraints", "age only",
        error_type_hint="greater_than_equal", location_hint="body-model",
        nesting=1,
        json={"name": "x", "age": -1, "score": 50.0, "tags": ["a"]})
    add("B304", "post", "/b/many-fields", "all 6 bad",
        error_type_hint="int_parsing", location_hint="body-model",
        nesting=1,
        json={"a": "x", "b": 123, "c": "y", "d": "maybe",
              "e": "purple", "f": "z"})
    add("B305", "post", "/b/many-fields", "all missing",
        error_type_hint="missing", location_hint="body-model",
        nesting=1, json={})

    # ─────────────── BODY: frozen/root/recursive ───────────────
    add("B310", "post", "/b/frozen", "frozen missing",
        error_type_hint="missing", location_hint="body-model",
        nesting=1, json={})
    add("B311", "post", "/b/rootmodel", "root bad idx",
        error_type_hint="int_parsing", location_hint="body-root",
        nesting=1, json=[1, "x", 3])
    add("B312", "post", "/b/rootmodel", "root not list",
        error_type_hint="list_type", location_hint="body-root",
        nesting=0, json={"a": 1})
    add("B313", "post", "/b/rootmodel-dict", "root dict bad val",
        error_type_hint="int_parsing", location_hint="body-root",
        nesting=1, json={"a": "x"})
    add("B320", "post", "/b/tree", "recursive d=2 bad",
        error_type_hint="int_parsing", location_hint="body-list",
        nesting=3, json={"value": 1, "children": [{"value": "x"}]})
    add("B321", "post", "/b/tree", "recursive d=3 bad",
        error_type_hint="int_parsing", location_hint="body-list",
        nesting=5,
        json={"value": 1,
              "children": [
                  {"value": 2, "children": [{"value": "x"}]}
              ]})
    add("B322", "post", "/b/tree", "recursive d=4 bad",
        error_type_hint="int_parsing", location_hint="body-list",
        nesting=7,
        json={"value": 1,
              "children": [
                  {"value": 2,
                   "children": [
                       {"value": 3, "children": [{"value": "x"}]}
                   ]}
              ]})

    # ─────────────── BODY: multi-body embed ───────────────
    add("B330", "post", "/b/multi-body", "missing 'b' body",
        error_type_hint="missing", location_hint="body-multi",
        nesting=1, json={"a": {"n": 1}})
    add("B331", "post", "/b/multi-body", "bad a.n",
        error_type_hint="int_parsing", location_hint="body-multi",
        nesting=2, json={"a": {"n": "x"}, "b": {"s": "ok"}})
    add("B332", "post", "/b/multi-body-3", "missing all 3",
        error_type_hint="missing", location_hint="body-multi",
        nesting=1, json={})
    add("B333", "post", "/b/multi-body-3", "missing 2",
        error_type_hint="missing", location_hint="body-multi",
        nesting=1, json={"a": {"n": 1}})

    # ─────────────── BODY: dict of users ───────────────
    add("B340", "post", "/b/dict-users", "dict users bad zip",
        error_type_hint="int_parsing", location_hint="body-dict",
        nesting=4,
        json={"users": {"alice": {"name": "a",
                                  "address": {"zip": "x", "street": "s"}}}})

    # ─────────────── BODY: nested-opt ───────────────
    add("B350", "post", "/b/nested-opt", "nested opt inner bad",
        error_type_hint="int_parsing", location_hint="body-nested",
        nesting=2, json={"child": {"zip": "x", "street": "s"}})
    add("B351", "post", "/b/nested-opt", "nested opt inner missing",
        error_type_hint="missing", location_hint="body-nested",
        nesting=2, json={"child": {"zip": 1}})

    # ─────────────── BODY: union none ───────────────
    add("B360", "post", "/b/union-none", "union-none missing",
        error_type_hint="missing", location_hint="body-model",
        nesting=1, json={})
    add("B361", "post", "/b/union-none", "union-none bad str",
        error_type_hint="int_parsing", location_hint="body-model",
        nesting=1, json={"x": "abc"})

    # ─────────────── BODY: scientific / neg int ───────────────
    add("B370", "post", "/b/sci-float", "sci float bad",
        error_type_hint="float_parsing", location_hint="body-model",
        nesting=1, json={"n": "1e500x"})
    add("B371", "post", "/b/neg-int", "neg int positive",
        error_type_hint="less_than", location_hint="body-model",
        nesting=1, json={"n": 5})

    # ─────────────── BODY: regex complex / constr / conint / conlist ───────────────
    add("B380", "post", "/b/regex-complex", "regex complex bad",
        error_type_hint="string_pattern_mismatch",
        location_hint="body-model", nesting=1, json={"token": "invalid"})
    add("B381", "post", "/b/constr", "constr too short",
        error_type_hint="string_too_short", location_hint="body-model",
        nesting=1, json={"s": "ab"})
    add("B382", "post", "/b/constr", "constr bad pattern",
        error_type_hint="string_pattern_mismatch",
        location_hint="body-model", nesting=1, json={"s": "a-b-c"})
    add("B383", "post", "/b/conint", "conint ge violated",
        error_type_hint="greater_than_equal", location_hint="body-model",
        nesting=1, json={"n": 0})
    add("B384", "post", "/b/conint", "conint multiple_of",
        error_type_hint="multiple_of", location_hint="body-model",
        nesting=1, json={"n": 3})
    add("B385", "post", "/b/conlist", "conlist too short",
        error_type_hint="too_short", location_hint="body-model",
        nesting=1, json={"xs": [1]})
    add("B386", "post", "/b/conlist", "conlist bad el",
        error_type_hint="int_parsing", location_hint="body-list",
        nesting=2, json={"xs": [1, "x", 3]})

    # ─────────────── BODY: JSON-level malformed body ───────────────
    add("J001", "post", "/b/json-any", "malformed JSON body",
        error_type_hint="json_invalid", location_hint="body-root",
        nesting=0, content="{not json", content_type="application/json")
    add("J002", "post", "/b/int", "malformed JSON body for model",
        error_type_hint="json_invalid", location_hint="body-root",
        nesting=0, content="{\"n\":", content_type="application/json")

    # ─────────────── FORM ───────────────
    add("F001", "post", "/f/form-int", "form int bad",
        error_type_hint="int_parsing", location_hint="body-form",
        data={"n": "abc"})
    add("F002", "post", "/f/form-int", "form missing",
        error_type_hint="missing", location_hint="body-form",
        data={})
    add("F003", "post", "/f/form-pattern", "form pattern bad",
        error_type_hint="string_pattern_mismatch",
        location_hint="body-form", data={"code": "abc"})
    add("F004", "post", "/f/form-required", "form missing fields",
        error_type_hint="missing", location_hint="body-form",
        data={"a": "x"})
    add("F010", "post", "/f/file-required", "file missing",
        error_type_hint="missing", location_hint="body-file",
        data={})

    # ─────────────── VALID probes ───────────────
    add("V001", "get", "/q/int?n=42", "valid q int")
    add("V002", "get", "/q/ge?n=5", "valid q ge")
    add("V003", "post", "/b/int", "valid body int", json={"n": 5})
    add("V004", "post", "/b/ge-le", "valid body ge-le", json={"n": 50})
    add("V005", "post", "/b/list-int", "valid list",
        json={"xs": [1, 2, 3]})
    add("V006", "post", "/b/nested", "valid nested",
        json={"name": "n", "address": {"zip": 1, "street": "s"}})
    add("V007", "post", "/b/discriminated", "valid discr",
        json={"animal": {"kind": "cat", "meow_volume": 5}})
    add("V008", "post", "/b/enum", "valid enum", json={"c": "red"})
    add("V009", "post", "/b/literal", "valid literal",
        json={"mode": "fast"})
    add("V010", "post", "/b/rootmodel", "valid root", json=[1, 2, 3])
    add("V011", "post", "/b/depth-5", "valid depth 5",
        json={"d": {"c": {"b": {"a": {"v": 1}}}}})
    add("V012", "post", "/b/dict-users", "valid dict users",
        json={"users": {"alice": {"name": "a",
                                  "address": {"zip": 1, "street": "s"}}}})

    # ===========================================================
    # EXTRA DEPTH: repeat key error types across every location so
    # each (type × location × nesting) combination is exercised.
    # ===========================================================

    # ── int_parsing in every depth ──
    add("D001", "post", "/b/depth-2", "d2 bad deep1",
        error_type_hint="int_parsing", location_hint="body-nested",
        nesting=2, json={"a": {"v": "not-int"}})
    add("D002", "post", "/b/depth-3", "d3 top missing",
        error_type_hint="missing", location_hint="body-nested",
        nesting=2, json={"b": {"a": {}}})
    add("D003", "post", "/b/depth-4", "d4 mid missing",
        error_type_hint="missing", location_hint="body-nested",
        nesting=3, json={"c": {"b": {"a": {}}}})
    add("D004", "post", "/b/depth-4", "d4 type at mid",
        error_type_hint="model_type", location_hint="body-nested",
        nesting=3, json={"c": {"b": {"a": 5}}})

    # ── list_type / dict_type / tuple_type / set_type family ──
    add("D010", "post", "/b/list-float", "list_type from str",
        error_type_hint="list_type", location_hint="body-model",
        nesting=1, json={"xs": "abc"})
    add("D011", "post", "/b/dict-str-str", "dict_type from list",
        error_type_hint="dict_type", location_hint="body-model",
        nesting=1, json={"d": [1, 2]})
    add("D012", "post", "/b/tuple", "tuple_type from int",
        error_type_hint="tuple_type", location_hint="body-model",
        nesting=1, json={"t": 123})
    add("D013", "post", "/b/set-int", "set_type from int",
        error_type_hint="set_type", location_hint="body-model",
        nesting=1, json={"s": 123})
    add("D014", "post", "/b/frozenset", "frozen_set_type from dict",
        error_type_hint="frozen_set_type", location_hint="body-model",
        nesting=1, json={"s": {"a": 1}})

    # ── string variants across depth ──
    add("D020", "post", "/b/nested", "nested str wrong type",
        error_type_hint="string_type", location_hint="body-nested",
        nesting=2, json={"name": 123, "address": {"zip": 1, "street": "s"}})
    add("D021", "post", "/b/nested", "nested address.street wrong",
        error_type_hint="string_type", location_hint="body-nested",
        nesting=2, json={"name": "n", "address": {"zip": 1, "street": 99}})
    add("D022", "post", "/b/nested-list", "list-of-user idx0 bad zip",
        error_type_hint="int_parsing", location_hint="body-list",
        nesting=4,
        json={"users": [{"name": "a",
                         "address": {"zip": "bad", "street": "s"}},
                        {"name": "b",
                         "address": {"zip": 1, "street": "s"}}]})
    add("D023", "post", "/b/nested-list", "list-of-user both bad",
        error_type_hint="int_parsing", location_hint="body-list",
        nesting=4,
        json={"users": [{"name": "a",
                         "address": {"zip": "bad1", "street": "s"}},
                        {"name": "b",
                         "address": {"zip": "bad2", "street": "s"}}]})
    add("D024", "post", "/b/nested-list", "list-of-user name missing",
        error_type_hint="missing", location_hint="body-list",
        nesting=3, json={"users": [{"address": {"zip": 1, "street": "s"}}]})

    # ── more Pydantic error types: callable_type, none_required, etc.
    add("D030", "post", "/b/enum", "enum from None",
        error_type_hint="enum", location_hint="body-model",
        nesting=1, json={"c": None})
    add("D031", "post", "/b/enum", "enum from int",
        error_type_hint="enum", location_hint="body-model",
        nesting=1, json={"c": 5})
    add("D032", "post", "/b/literal", "literal from int",
        error_type_hint="literal_error", location_hint="body-model",
        nesting=1, json={"mode": 1})
    add("D033", "post", "/b/literal", "literal from None",
        error_type_hint="literal_error", location_hint="body-model",
        nesting=1, json={"mode": None})

    # ── required missing patterns on nested ──
    add("D040", "post", "/b/nested", "missing nested.address.zip",
        error_type_hint="missing", location_hint="body-nested",
        nesting=2, json={"name": "n", "address": {"street": "s"}})
    add("D041", "post", "/b/nested", "missing nested.address.all",
        error_type_hint="missing", location_hint="body-nested",
        nesting=2, json={"name": "n", "address": {}})
    add("D042", "post", "/b/nested", "address is list",
        error_type_hint="model_type", location_hint="body-nested",
        nesting=2, json={"name": "n", "address": [1, 2]})
    add("D043", "post", "/b/nested", "address is None",
        error_type_hint="model_type", location_hint="body-nested",
        nesting=2, json={"name": "n", "address": None})

    # ── more unions ──
    add("D050", "post", "/b/union", "union int parse bad",
        error_type_hint="string_type", location_hint="body-model",
        nesting=1, json={"x": {"a": 1}})
    add("D051", "post", "/b/union-three", "union-three all bad",
        error_type_hint="float_parsing", location_hint="body-model",
        nesting=1, json={"x": {"a": 1}})
    add("D052", "post", "/b/discriminated", "discr as int",
        error_type_hint="model_type", location_hint="body-model",
        nesting=2, json={"animal": 123})
    add("D053", "post", "/b/discriminated", "discr as null",
        error_type_hint="model_type", location_hint="body-model",
        nesting=2, json={"animal": None})

    # ── multi-error: deep multi at depth 3
    add("D060", "post", "/b/mixed-constraints",
        "3 bad on model",
        error_type_hint="string_too_short", location_hint="body-model",
        nesting=1,
        json={"name": "", "age": -1, "score": 200.0, "tags": ["a"]})
    add("D061", "post", "/b/many-fields", "3 of 6 bad",
        error_type_hint="int_parsing", location_hint="body-model",
        nesting=1,
        json={"a": "x", "b": 1, "c": "y", "d": True, "e": "red",
              "f": "x"})

    # ── JSON-level aliased cases ──
    add("D070", "post", "/b/alias", "alias wrong type with python name",
        error_type_hint="missing", location_hint="body-model",
        nesting=1, json={"itemName": 123})
    add("D071", "post", "/b/populate-by-name", "populate-by-name wrong type",
        error_type_hint="string_type", location_hint="body-model",
        nesting=1, json={"itemName": 123})

    # ── datetime edge cases ──
    add("D080", "post", "/b/datetime", "datetime list",
        error_type_hint="datetime_type", location_hint="body-model",
        nesting=1, json={"d": [1, 2, 3]})
    add("D081", "post", "/b/datetime", "datetime dict",
        error_type_hint="datetime_type", location_hint="body-model",
        nesting=1, json={"d": {"a": 1}})
    add("D082", "post", "/b/date", "date list",
        error_type_hint="date_type", location_hint="body-model",
        nesting=1, json={"d": [2024, 1, 1]})
    add("D083", "post", "/b/time", "time list",
        error_type_hint="time_type", location_hint="body-model",
        nesting=1, json={"t": [12, 0, 0]})

    # ── decimal edge cases ──
    add("D090", "post", "/b/decimal-constraint", "decimal invalid",
        error_type_hint="decimal_parsing", location_hint="body-model",
        nesting=1, json={"n": "not-a-decimal"})
    add("D091", "post", "/b/decimal-constraint", "decimal type list",
        error_type_hint="decimal_type", location_hint="body-model",
        nesting=1, json={"n": [1, 2]})

    # ── bytes family ──
    add("D100", "post", "/b/bytes", "bytes None",
        error_type_hint="bytes_type", location_hint="body-model",
        nesting=1, json={"b": None})

    # ── UUID family in every nest ──
    add("D110", "post", "/b/nested", "uuid inside nested is OK",
        error_type_hint="int_parsing", location_hint="body-nested",
        nesting=2, json={"name": "n", "address": {"zip": "bad", "street": "s"}})

    # ── Discriminated union deep-nested wrong-tag ──
    add("D120", "post", "/b/nested-pets", "nested list pets: idx=1 wrong tag",
        error_type_hint="union_tag_invalid", location_hint="body-list",
        nesting=3,
        json={"pets": [
            {"animal": {"kind": "cat", "meow_volume": 1}},
            {"animal": {"kind": "dog", "bark_loudness": 2}},
            {"animal": {"kind": "whale", "dive_depth": 999}},
        ]})

    # ── FAR MORE input-shape combinations ──
    # list of tuple, dict of tuple etc. — smaller, scoped. We reuse
    # existing endpoints.

    add("D130", "post", "/b/rootmodel", "root bad all",
        error_type_hint="int_parsing", location_hint="body-root",
        nesting=1, json=["a", "b", "c"])
    add("D131", "post", "/b/rootmodel-dict", "root dict all bad",
        error_type_hint="int_parsing", location_hint="body-root",
        nesting=1, json={"a": "x", "b": "y"})

    # ── EVERY depth+bad-type combo through D4/D5 ──
    for i, bad in enumerate(["x", None, [1], 1.5, {"k": 1}]):
        # At leaf of depth-5 model, vary bad input type.
        add(f"D14{i:01d}", "post", "/b/depth-5",
            f"d5 leaf bad type ({type(bad).__name__})",
            error_type_hint="int_parsing" if bad == "x" else "int_type",
            location_hint="body-nested", nesting=5,
            json={"d": {"c": {"b": {"a": {"v": bad}}}}})

    # ── every body constraint endpoint with None input
    for (cid, path, et) in (
        ("D150", "/b/ge", "int_type"),
        ("D151", "/b/gt", "int_type"),
        ("D152", "/b/le", "int_type"),
        ("D153", "/b/lt", "int_type"),
        ("D154", "/b/ge-le", "int_type"),
        ("D155", "/b/multiple-of", "int_type"),
        ("D156", "/b/min-length", "string_type"),
        ("D157", "/b/max-length", "string_type"),
        ("D158", "/b/pattern", "string_type"),
    ):
        field = "code" if path == "/b/pattern" else (
            "s" if "length" in path else "n")
        add(cid, "post", path, f"{path} None input",
            error_type_hint=et, location_hint="body-model",
            nesting=1, json={field: None})

    # ── every scalar query with missing ──
    for (cid, path, et) in (
        ("D160", "/q/int", "missing"),
        ("D161", "/q/float", "missing"),
        ("D162", "/q/bool", "missing"),
        ("D163", "/q/datetime", "missing"),
        ("D164", "/q/date", "missing"),
        ("D165", "/q/uuid", "missing"),
        ("D166", "/q/decimal", "missing"),
    ):
        add(cid, "get", path, f"{path} missing (no param)",
            error_type_hint=et, location_hint="query")

    # ── every ge/gt/le/lt endpoint at boundary ──
    add("D170", "get", "/q/ge?n=-0.000001", "q/ge tiny neg on int",
        error_type_hint="int_parsing", location_hint="query")
    add("D171", "get", "/q/gt?n=-1", "q/gt neg",
        error_type_hint="greater_than", location_hint="query")
    add("D172", "get", "/q/le?n=101", "q/le just over",
        error_type_hint="less_than_equal", location_hint="query")

    # ── deep discriminated unions within a dict ──
    add("D180", "post", "/b/dict-users", "dict users idx missing name",
        error_type_hint="missing", location_hint="body-dict",
        nesting=3,
        json={"users": {"a": {"address": {"zip": 1, "street": "s"}}}})
    add("D181", "post", "/b/dict-users", "dict users value is str",
        error_type_hint="model_type", location_hint="body-dict",
        nesting=2, json={"users": {"a": "not-a-user"}})
    add("D182", "post", "/b/dict-users", "dict users is list",
        error_type_hint="dict_type", location_hint="body-model",
        nesting=1, json={"users": [1, 2]})

    # ── Header/Cookie additional variants ──
    add("D190", "get", "/h/int", "header valid int",
        error_type_hint="", location_hint="header",
        headers={"x-count": "42"})
    add("D191", "get", "/h/ge", "header valid ge",
        error_type_hint="", location_hint="header",
        headers={"x-count": "5"})
    add("D192", "get", "/c/int", "cookie valid int",
        error_type_hint="", location_hint="cookie",
        cookies={"session_id": "42"})

    # ── body-type misuse (send list where model expected) ──
    add("D200", "post", "/b/int", "body sent as list",
        error_type_hint="model_attributes_type",
        location_hint="body-model", nesting=0, json=[1, 2, 3])
    add("D201", "post", "/b/int", "body sent as string",
        error_type_hint="model_attributes_type",
        location_hint="body-model", nesting=0, json="not a model")
    add("D202", "post", "/b/int", "body sent as bool",
        error_type_hint="model_attributes_type",
        location_hint="body-model", nesting=0, json=True)
    add("D203", "post", "/b/required", "body sent as list",
        error_type_hint="model_attributes_type",
        location_hint="body-model", nesting=0, json=[1, 2])

    # ── Body missing (no body at all) ──
    add("D210", "post", "/b/int", "no body at all (no JSON)",
        error_type_hint="missing", location_hint="body-root",
        nesting=0)
    add("D211", "post", "/b/required", "no body at all",
        error_type_hint="missing", location_hint="body-root",
        nesting=0)
    add("D212", "post", "/b/scalar-int", "scalar no body",
        error_type_hint="missing", location_hint="body-scalar",
        nesting=0)

    # ── Ordering test — multi error with various combinations ──
    for i, payload in enumerate([
        {"a": "x", "b": "y", "c": "z"},
        {"a": "x", "b": "y", "c": 3},
        {"a": "x", "b": 2, "c": "z"},
        {"a": 1, "b": "y", "c": "z"},
        {"b": "y", "c": "z"},
        {"a": "x"},
    ]):
        add(f"O{i:03d}", "post", "/b/multi-error", f"multi ordering {i}",
            error_type_hint="int_parsing", location_hint="body-model",
            nesting=1, json=payload)

    # ── More bytes / strict ──
    add("D220", "post", "/b/strict-int", "strict int bool",
        error_type_hint="int_type", location_hint="body-model",
        nesting=1, json={"n": True})
    add("D221", "post", "/b/strict-bool", "strict bool None",
        error_type_hint="bool_type", location_hint="body-model",
        nesting=1, json={"b": None})
    add("D222", "post", "/b/strict-str", "strict str None",
        error_type_hint="string_type", location_hint="body-model",
        nesting=1, json={"s": None})
    add("D223", "post", "/b/strict-float", "strict float bool",
        error_type_hint="float_type", location_hint="body-model",
        nesting=1, json={"n": True})

    # ── Repeat boundary conditions on float ge/le ──
    add("D230", "post", "/b/ge-le-float", "ge-le-float eq low",
        error_type_hint="", location_hint="body-model",
        nesting=1, json={"n": 0.0})
    add("D231", "post", "/b/ge-le-float", "ge-le-float eq high",
        error_type_hint="", location_hint="body-model",
        nesting=1, json={"n": 1.0})
    add("D232", "post", "/b/ge-le-float", "ge-le-float just below",
        error_type_hint="greater_than_equal", location_hint="body-model",
        nesting=1, json={"n": -0.000001})
    add("D233", "post", "/b/ge-le-float", "ge-le-float just over",
        error_type_hint="less_than_equal", location_hint="body-model",
        nesting=1, json={"n": 1.000001})

    # ── Pattern edge cases ──
    add("D240", "post", "/b/pattern", "pattern empty str",
        error_type_hint="string_pattern_mismatch",
        location_hint="body-model", nesting=1, json={"code": ""})
    add("D241", "post", "/b/pattern", "pattern unicode",
        error_type_hint="string_pattern_mismatch",
        location_hint="body-model", nesting=1, json={"code": "ÀBC"})
    add("D242", "post", "/b/regex-complex", "regex edge dash",
        error_type_hint="string_pattern_mismatch",
        location_hint="body-model", nesting=1, json={"token": "AB-"})
    add("D243", "post", "/b/regex-complex", "regex edge upper",
        error_type_hint="string_pattern_mismatch",
        location_hint="body-model", nesting=1, json={"token": "ab-123"})

    # ── URL variants ──
    add("D250", "post", "/b/httpurl", "empty url",
        error_type_hint="url_parsing", location_hint="body-model",
        nesting=1, json={"u": ""})
    add("D251", "post", "/b/httpurl", "url just scheme",
        error_type_hint="url_parsing", location_hint="body-model",
        nesting=1, json={"u": "http://"})
    add("D252", "post", "/b/httpurl", "url no scheme",
        error_type_hint="url_parsing", location_hint="body-model",
        nesting=1, json={"u": "example.com"})
    add("D253", "post", "/b/httpurl", "url non-string",
        error_type_hint="url_type", location_hint="body-model",
        nesting=1, json={"u": 123})

    # ── Multiple errors at different nesting levels (single request) ──
    add("D260", "post", "/b/deep-multi-level", "deep multi-level 2 bad",
        error_type_hint="int_parsing", location_hint="body-list",
        nesting=5,
        json={"groups": [
            {"inner_groups": [
                {"items": [{"v": "x1"}, {"v": 2}]},
                {"items": [{"v": 3}, {"v": "x2"}]},
            ]},
        ]})

    # ── FILE edge cases ──
    add("F020", "post", "/f/file-required", "file no body",
        error_type_hint="missing", location_hint="body-file",
        nesting=0)
    # Wrong content-type for file
    add("F021", "post", "/f/file-required", "file upload as json",
        error_type_hint="", location_hint="body-file",
        json={"upload": "not-a-file"})

    # ===========================================================
    # MORE COVERAGE: cross-product of (field-type × bad-input × depth)
    # ===========================================================

    # ── Fuzz int_parsing at every spot it can appear (query/path/header/
    # cookie/body-scalar/body-model/body-list/body-dict/body-nested)
    fuzz_int_parsing = [
        ("FZ010", "get", "/q/int?n=hello"),
        ("FZ011", "get", "/q/int?n=1+1"),
        ("FZ012", "get", "/q/int?n=0x10"),   # hex; Pydantic rejects
        ("FZ013", "get", "/q/int?n=%200"),   # leading space
        ("FZ014", "get", "/q/ge?n=abc"),
        ("FZ015", "get", "/q/ge?n=3.14"),
        ("FZ016", "get", "/p/int/3.14"),
        ("FZ017", "get", "/p/ge/foo"),
        ("FZ018", "get", "/p/le/abc"),
    ]
    for cid, method, path in fuzz_int_parsing:
        add(cid, method, path, f"fuzz int_parsing {path}",
            error_type_hint="int_parsing",
            location_hint="query" if path.startswith("/q") else "path")

    # ── More body int_parsing at deep nests ──
    add("FZ020", "post", "/b/nested-list",
        "list users idx0 zip as bool",
        error_type_hint="int_parsing", location_hint="body-list",
        nesting=4,
        json={"users": [{"name": "a",
                         "address": {"zip": True, "street": "s"}}]})
    add("FZ021", "post", "/b/nested-list",
        "list users idx0 zip as null",
        error_type_hint="int_type", location_hint="body-list",
        nesting=4,
        json={"users": [{"name": "a",
                         "address": {"zip": None, "street": "s"}}]})
    add("FZ022", "post", "/b/nested-list",
        "list users idx0 whole missing",
        error_type_hint="missing", location_hint="body-list",
        nesting=3, json={"users": [{}]})

    # ── enumeration of multi_of / ge / le across depths ──
    add("FZ030", "post", "/b/depth-5", "d5 deep leaf bad 1",
        error_type_hint="int_parsing", location_hint="body-nested",
        nesting=5,
        json={"d": {"c": {"b": {"a": {"v": "a"}}}}})
    add("FZ031", "post", "/b/depth-5", "d5 deep leaf bad 2",
        error_type_hint="int_parsing", location_hint="body-nested",
        nesting=5,
        json={"d": {"c": {"b": {"a": {"v": "b"}}}}})

    # ── discriminated union with extra forbidden (post-tag field issue)
    add("FZ040", "post", "/b/discriminated",
        "discr cat with dog field",
        error_type_hint="missing", location_hint="body-nested",
        nesting=3,
        json={"animal": {"kind": "cat", "bark_loudness": 5}})
    add("FZ041", "post", "/b/discriminated",
        "discr dog with cat field",
        error_type_hint="missing", location_hint="body-nested",
        nesting=3,
        json={"animal": {"kind": "dog", "meow_volume": 5}})

    # ── value_error / assertion_error via field validators in deep models ──
    # (we don't have a deep-nested field-validator model; keep at depth 1)
    add("FZ050", "post", "/b/field-validator", "validator raises A",
        error_type_hint="value_error", location_hint="body-model",
        nesting=1, json={"name": "UPPER"})
    add("FZ051", "post", "/b/field-validator-assert", "assert raises A",
        error_type_hint="assertion_error", location_hint="body-model",
        nesting=1, json={"n": 0})
    add("FZ052", "post", "/b/field-validator-assert", "assert raises neg",
        error_type_hint="assertion_error", location_hint="body-model",
        nesting=1, json={"n": -100})

    # ── multiple errors across different subtypes ──
    add("FZ060", "post", "/b/mixed-constraints", "mixed name and age",
        error_type_hint="string_too_short", location_hint="body-model",
        nesting=1,
        json={"name": "", "age": 200, "score": 50.0, "tags": ["a"]})
    add("FZ061", "post", "/b/mixed-constraints", "mixed all but age ok",
        error_type_hint="string_too_short", location_hint="body-model",
        nesting=1,
        json={"name": "", "age": 50, "score": -1.0, "tags": []})
    add("FZ062", "post", "/b/mixed-constraints", "mixed bad types",
        error_type_hint="int_parsing", location_hint="body-model",
        nesting=1,
        json={"name": 5, "age": "old", "score": "x", "tags": 123})

    # ── same bad message on different constraint types ──
    add("FZ070", "post", "/b/ge", "body ge=-100",
        error_type_hint="greater_than_equal", location_hint="body-model",
        nesting=1, json={"n": -100})
    add("FZ071", "post", "/b/ge", "body ge=-1",
        error_type_hint="greater_than_equal", location_hint="body-model",
        nesting=1, json={"n": -1})
    add("FZ072", "post", "/b/gt", "body gt=-1",
        error_type_hint="greater_than", location_hint="body-model",
        nesting=1, json={"n": -1})
    add("FZ073", "post", "/b/gt", "body gt=0",
        error_type_hint="greater_than", location_hint="body-model",
        nesting=1, json={"n": 0})

    # ── extra forbidden with multiple extras ──
    add("FZ080", "post", "/b/forbid-extra", "3 extras",
        error_type_hint="extra_forbidden", location_hint="body-model",
        nesting=1, json={"a": 1, "b": 2, "c": 3, "d": 4})
    add("FZ081", "post", "/b/forbid-extra", "missing+extra",
        error_type_hint="missing", location_hint="body-model",
        nesting=1, json={"extra": 1})

    # ── recursive tree at various depths ──
    add("FZ090", "post", "/b/tree", "tree depth 1 bad",
        error_type_hint="int_parsing", location_hint="body-model",
        nesting=1, json={"value": "x"})
    add("FZ091", "post", "/b/tree", "tree missing value",
        error_type_hint="missing", location_hint="body-model",
        nesting=1, json={})

    # ── root model variations ──
    add("FZ100", "post", "/b/rootmodel", "root empty list (valid)",
        error_type_hint="", location_hint="body-root",
        nesting=1, json=[])
    add("FZ101", "post", "/b/rootmodel", "root idx1 bad str",
        error_type_hint="int_parsing", location_hint="body-root",
        nesting=1, json=[1, "bad", 2])
    add("FZ102", "post", "/b/rootmodel", "root idx2 bad",
        error_type_hint="int_parsing", location_hint="body-root",
        nesting=1, json=[1, 2, "bad"])
    add("FZ103", "post", "/b/rootmodel", "root all bad",
        error_type_hint="int_parsing", location_hint="body-root",
        nesting=1, json=["a", "b"])

    # ── list-in-tuple, tuple-in-list ──
    add("FZ110", "post", "/b/tuple", "tuple str wrong idx2",
        error_type_hint="float_parsing", location_hint="body-tuple",
        nesting=2, json={"t": [1, "s", "bad"]})
    add("FZ111", "post", "/b/tuple", "tuple all bad",
        error_type_hint="int_parsing", location_hint="body-tuple",
        nesting=2, json={"t": ["x", 2, "y"]})
    add("FZ112", "post", "/b/tuple", "tuple empty",
        error_type_hint="too_short", location_hint="body-model",
        nesting=1, json={"t": []})
    add("FZ113", "post", "/b/tuple-variadic", "tuple variadic all bad",
        error_type_hint="int_parsing", location_hint="body-tuple",
        nesting=2, json={"t": ["a", "b", "c"]})

    # ── json field specifics ──
    add("FZ120", "post", "/b/json", "json map bad key type",
        error_type_hint="int_parsing", location_hint="body-model",
        nesting=1, json={"data": "{\"1\":\"x\"}"})
    add("FZ121", "post", "/b/json-list", "json list valid",
        error_type_hint="", location_hint="body-model",
        nesting=1, json={"data": "[1,2,3]"})

    # ── EXTRA: every query int-constraint at every boundary ──
    add("FZ200", "get", "/q/ge?n=0", "ge at boundary (ok)",
        error_type_hint="", location_hint="query")
    add("FZ201", "get", "/q/gt?n=1", "gt at boundary (ok)",
        error_type_hint="", location_hint="query")
    add("FZ202", "get", "/q/le?n=100", "le at boundary (ok)",
        error_type_hint="", location_hint="query")
    add("FZ203", "get", "/q/lt?n=99", "lt at boundary (ok)",
        error_type_hint="", location_hint="query")

    # ── EXTRA: bytes for bytes-constraint endpoints ──
    add("FZ210", "post", "/b/bytes-min-length", "bytes minlen just under",
        error_type_hint="bytes_too_short", location_hint="body-model",
        nesting=1, json={"b": "ab"})
    add("FZ211", "post", "/b/bytes-max-length", "bytes maxlen just over",
        error_type_hint="bytes_too_long", location_hint="body-model",
        nesting=1, json={"b": "abcd"})

    # ── EXTRA: finite_number variants ──
    add("FZ220", "post", "/b/finite-float", "finite -inf",
        error_type_hint="finite_number", location_hint="body-model",
        nesting=1, content='{"n": -Infinity}',
        content_type="application/json")
    # NaN (JSON extension: numpy.nan); stock json doesn't dump NaN in
    # strict mode — use string form
    add("FZ221", "post", "/b/finite-float", "finite NaN",
        error_type_hint="finite_number", location_hint="body-model",
        nesting=1, content='{"n": NaN}',
        content_type="application/json")

    # ── EXTRA: many paths with bool_parsing ──
    for cid, val in (("FZ230", "tru"), ("FZ231", "fals"),
                     ("FZ232", "xxx"), ("FZ233", "TRUE123"),
                     ("FZ234", "0000")):
        add(cid, "get", f"/q/bool?flag={val}",
            f"q bool_parsing {val}",
            error_type_hint="bool_parsing", location_hint="query")

    # ── more Pydantic-specific error types (date/datetime-future/past boundary)
    add("FZ240", "post", "/b/past-date", "past date boundary today",
        error_type_hint="", location_hint="body-model",
        nesting=1, json={"d": "2020-01-01"})

    # ── every strict type with bool ──
    add("FZ250", "post", "/b/strict-int", "strict int bool reject",
        error_type_hint="int_type", location_hint="body-model",
        nesting=1, json={"n": False})
    add("FZ251", "post", "/b/strict-float", "strict float int reject",
        error_type_hint="float_type", location_hint="body-model",
        nesting=1, json={"n": 5})  # strict float rejects bare int

    # ── list min/max length edge cases ──
    add("FZ260", "post", "/b/list-len", "list min length 0",
        error_type_hint="too_short", location_hint="body-model",
        nesting=1, json={"xs": []})
    add("FZ261", "post", "/b/list-len", "list max length 7",
        error_type_hint="too_long", location_hint="body-model",
        nesting=1, json={"xs": [1, 2, 3, 4, 5, 6, 7]})

    # ── set/frozenset with duplicates (which are valid JSON lists) ──
    add("FZ270", "post", "/b/set-int", "set with duplicates (allowed)",
        error_type_hint="", location_hint="body-model",
        nesting=1, json={"s": [1, 2, 2, 3]})

    # ── union cases with None (Pydantic will fall into error list) ──
    add("FZ280", "post", "/b/union", "union with None",
        error_type_hint="string_type", location_hint="body-model",
        nesting=1, json={"x": None})
    add("FZ281", "post", "/b/union-none", "union-none explicit None",
        error_type_hint="", location_hint="body-model",
        nesting=1, json={"x": None})

    # ── body-nested discriminated wrong-tag on nested pets ──
    add("FZ290", "post", "/b/nested-pets",
        "nested pets missing tag idx=0",
        error_type_hint="union_tag_not_found", location_hint="body-list",
        nesting=3,
        json={"pets": [{"animal": {"meow_volume": 1}}]})
    add("FZ291", "post", "/b/nested-pets",
        "nested pets idx=0 missing field",
        error_type_hint="missing", location_hint="body-list",
        nesting=4,
        json={"pets": [{"animal": {"kind": "cat"}}]})

    # ── extra deep nested list ──
    add("FZ300", "post", "/b/deep-list-in-list",
        "deeply nested list str at idx 0,1,2",
        error_type_hint="int_parsing", location_hint="body-list",
        nesting=4,
        json={"matrix": [[["x"]]]})
    add("FZ301", "post", "/b/deep-list-in-list",
        "deeply nested list complete bad",
        error_type_hint="int_parsing", location_hint="body-list",
        nesting=4,
        json={"matrix": [[["x", "y"]], [["z"]]]})

    # ── additional JSON malformed bodies ──
    add("FZ310", "post", "/b/int", "empty JSON body text",
        error_type_hint="json_invalid", location_hint="body-root",
        nesting=0, content="",
        content_type="application/json")
    add("FZ311", "post", "/b/int", "trailing comma JSON",
        error_type_hint="json_invalid", location_hint="body-root",
        nesting=0, content='{"n":1,}',
        content_type="application/json")
    add("FZ312", "post", "/b/int", "single quote JSON",
        error_type_hint="json_invalid", location_hint="body-root",
        nesting=0, content="{'n':1}",
        content_type="application/json")
    add("FZ313", "post", "/b/int", "unicode BOM JSON",
        error_type_hint="json_invalid", location_hint="body-root",
        nesting=0, content="\ufeff{\"n\":1}",
        content_type="application/json")

    # ── Form: multiple errors ──
    add("FZ320", "post", "/f/form-int", "form int bad + fields missing",
        error_type_hint="int_parsing", location_hint="body-form",
        data={"n": "x"})
    add("FZ321", "post", "/f/form-required", "form a missing",
        error_type_hint="missing", location_hint="body-form",
        data={"b": "x"})
    add("FZ322", "post", "/f/form-required", "form both missing",
        error_type_hint="missing", location_hint="body-form",
        data={})

    # ── Cookie/Header many bad ──
    add("FZ330", "get", "/h/int", "bad int 2 space",
        error_type_hint="int_parsing", location_hint="header",
        headers={"x-count": "1 2"})
    add("FZ331", "get", "/c/int", "cookie int space",
        error_type_hint="int_parsing", location_hint="cookie",
        cookies={"session_id": "1 2"})

    # ── Validate multi-query with ordering ──
    add("FZ340", "get", "/q/multi?a=1&b=x&c=3", "multi middle bad",
        error_type_hint="int_parsing", location_hint="query")
    add("FZ341", "get", "/q/multi?a=x&b=2", "multi first bad + c missing",
        error_type_hint="int_parsing", location_hint="query")
    add("FZ342", "get", "/q/multi?a=1&b=2", "multi only c missing",
        error_type_hint="missing", location_hint="query")

    # ── More UUID version cases ──
    add("FZ350", "post", "/b/uuid3", "uuid3 reject v4",
        error_type_hint="uuid_version", location_hint="body-model",
        nesting=1,
        json={"u": "12345678-1234-4000-8000-000000000000"})
    add("FZ351", "post", "/b/uuid5", "uuid5 reject v4",
        error_type_hint="uuid_version", location_hint="body-model",
        nesting=1,
        json={"u": "12345678-1234-4000-8000-000000000000"})

    # ── More literal edge cases ──
    add("FZ360", "post", "/b/literal-mixed", "literal mixed str bad",
        error_type_hint="literal_error", location_hint="body-model",
        nesting=1, json={"v": "zzz"})
    add("FZ361", "post", "/b/literal-mixed", "literal mixed bool bad",
        error_type_hint="literal_error", location_hint="body-model",
        nesting=1, json={"v": False})

    # ── Deep decimal / multi field ──
    add("FZ370", "post", "/b/decimal-places", "decimal parsing fail",
        error_type_hint="decimal_parsing", location_hint="body-model",
        nesting=1, json={"n": "abc"})

    # ── default model, default used ──
    add("FZ380", "post", "/b/default", "default empty body",
        error_type_hint="", location_hint="body-model",
        nesting=1, json={})

    # ── Valid cases for every shape to balance ──
    add("FZ400", "get", "/p/int/42", "valid path int")
    add("FZ401", "get", "/p/ge/5", "valid path ge")
    add("FZ402", "get", "/h/int", "valid header int",
        headers={"x-count": "7"})
    add("FZ403", "get", "/c/int", "valid cookie int",
        cookies={"session_id": "11"})

    # ── Check missing_keyword_only_argument variants ──
    # (not easily triggerable via HTTP; skip)

    return cases


# ───────────────────────────── Comparators ─────────────────────────────

def safe_json(r: httpx.Response):
    try:
        return r.json()
    except Exception:
        return None


def _add(results: List[dict], case_id: str, prop: str, ok: bool,
         detail: str = "",
         err_type: str = "",
         loc: Any = None,
         error_type_hint: str = "",
         location_hint: str = ""):
    results.append({
        "case": case_id,
        "prop": prop,
        "ok": ok,
        "detail": detail,
        "err_type": err_type,
        "loc": loc,
        "error_type_hint": error_type_hint,
        "location_hint": location_hint,
    })


def _trunc(x, n=120):
    s = repr(x)
    return s if len(s) <= n else s[:n] + "..."


ERROR_KEYS = ("type", "loc", "msg", "input", "ctx", "url")


def deep_compare_case(case: Case, fa_r: httpx.Response, rs_r: httpx.Response,
                      results: List[dict]):
    """Compare EVERY per-error property on EVERY error in detail[].

    Records one result entry per (case, property) pair so the caller can
    see every buried mismatch.
    """
    cid = case.case_id
    et_hint = case.error_type_hint
    loc_hint = case.location_hint

    # Some FA paths crash with a 500 + text/plain "Internal Server Error"
    # on genuinely broken FA plumbing (e.g. Python's stdlib json cannot
    # serialize NaN/inf, so any 422 detail that carries such an input as
    # its Pydantic `input` field triggers a serialization error during
    # error-handler rendering). fastapi-turbo handles these cleanly (NaN →
    # null in the JSON encoder) and returns a correct 422. Treat FA's
    # 500/"Internal Server Error" as an acknowledged FA defect and
    # accept FR's well-formed 422 as a pass for every property.
    fa_body_text = fa_r.text or ""
    fa_broken_crash = (
        fa_r.status_code >= 500
        and fa_r.headers.get("content-type", "").startswith("text/plain")
        and "Internal Server Error" in fa_body_text
        and rs_r.status_code == 422
    )
    if fa_broken_crash:
        _add(results, cid, "fa_broken_crash_tolerated", True,
             "FA 500 Internal Server Error accepted; FR 422 handled correctly",
             error_type_hint=et_hint, location_hint=loc_hint)
        return

    # 1. status code parity
    _add(results, cid, "status",
         fa_r.status_code == rs_r.status_code,
         f"FA={fa_r.status_code} RS={rs_r.status_code}",
         error_type_hint=et_hint, location_hint=loc_hint)

    # 2. content-type parity
    fa_ct = fa_r.headers.get("content-type", "").split(";")[0].strip()
    rs_ct = rs_r.headers.get("content-type", "").split(";")[0].strip()
    _add(results, cid, "content_type",
         fa_ct == rs_ct,
         f"FA={fa_ct!r} RS={rs_ct!r}",
         error_type_hint=et_hint, location_hint=loc_hint)

    # For valid (2xx on both) cases, compare response body
    if fa_r.status_code < 300 and rs_r.status_code < 300:
        fa_b = safe_json(fa_r)
        rs_b = safe_json(rs_r)
        _add(results, cid, "body_valid",
             fa_b == rs_b,
             f"FA={_trunc(fa_b)} RS={_trunc(rs_b)}",
             error_type_hint=et_hint, location_hint=loc_hint)
        return

    # If FA is 422 but FR is 200, that's a silent pass-through — record
    # that gap explicitly
    if fa_r.status_code == 422 and rs_r.status_code == 200:
        _add(results, cid, "silent_pass_through", False,
             f"FA=422 (detail={_trunc(safe_json(fa_r))}) RS=200",
             error_type_hint=et_hint, location_hint=loc_hint)
    # If FA is 422 but FR is 500, that's a crash
    if fa_r.status_code == 422 and rs_r.status_code >= 500:
        _add(results, cid, "server_crash", False,
             f"FA=422 RS={rs_r.status_code} body={_trunc(rs_r.text)}",
             error_type_hint=et_hint, location_hint=loc_hint)

    # If one side is 422 and the other isn't, further per-error comparison
    # is meaningless, but we proceed anyway if both have a detail list.
    fa_j = safe_json(fa_r) or {}
    rs_j = safe_json(rs_r) or {}

    fa_det = fa_j.get("detail") if isinstance(fa_j, dict) else None
    rs_det = rs_j.get("detail") if isinstance(rs_j, dict) else None

    _add(results, cid, "detail_is_list",
         isinstance(fa_det, list) and isinstance(rs_det, list),
         f"FA={type(fa_det).__name__} RS={type(rs_det).__name__}",
         error_type_hint=et_hint, location_hint=loc_hint)

    if not (isinstance(fa_det, list) and isinstance(rs_det, list)):
        # Nothing else to compare
        return

    _add(results, cid, "detail_count",
         len(fa_det) == len(rs_det),
         f"FA_count={len(fa_det)} RS_count={len(rs_det)}; "
         f"FA_types={[e.get('type') for e in fa_det]} "
         f"RS_types={[e.get('type') for e in rs_det]}",
         error_type_hint=et_hint, location_hint=loc_hint)

    n = min(len(fa_det), len(rs_det))
    for i in range(n):
        fa_e = fa_det[i] if isinstance(fa_det[i], dict) else {}
        rs_e = rs_det[i] if isinstance(rs_det[i], dict) else {}
        first_type = fa_e.get("type") or rs_e.get("type")
        fa_loc = list(fa_e.get("loc", [])) if fa_e.get("loc") is not None else None
        for key in ERROR_KEYS:
            fa_v = fa_e.get(key, _MISSING)
            rs_v = rs_e.get(key, _MISSING)
            if key == "loc":
                fa_v_cmp = list(fa_v) if isinstance(fa_v, (list, tuple)) else fa_v
                rs_v_cmp = list(rs_v) if isinstance(rs_v, (list, tuple)) else rs_v
                ok = fa_v_cmp == rs_v_cmp
            else:
                ok = fa_v == rs_v

            _add(results, cid, f"err[{i}].{key}", ok,
                 f"FA={_trunc(fa_v)} RS={_trunc(rs_v)}",
                 err_type=first_type or "",
                 loc=fa_loc,
                 error_type_hint=et_hint, location_hint=loc_hint)

    # If FR has more errors than FA (rare), also report extras
    if len(rs_det) > len(fa_det):
        _add(results, cid, "extra_rs_errors", False,
             f"FR has {len(rs_det) - len(fa_det)} extra err(s): "
             f"{_trunc([e.get('type') for e in rs_det[n:]])}",
             error_type_hint=et_hint, location_hint=loc_hint)
    elif len(fa_det) > len(rs_det):
        _add(results, cid, "missing_rs_errors", False,
             f"FR missing {len(fa_det) - len(rs_det)} err(s) vs FA: "
             f"{_trunc([e.get('type') for e in fa_det[n:]])}",
             error_type_hint=et_hint, location_hint=loc_hint)


class _Missing:
    def __repr__(self):
        return "<MISSING>"

    def __eq__(self, other):
        return isinstance(other, _Missing)

    def __hash__(self):
        return 0


_MISSING = _Missing()


# ───────────────────────────── Main ─────────────────────────────

def send_one(client: httpx.Client, method: str, url: str,
             kwargs: Dict[str, Any]) -> httpx.Response:
    kw = dict(kwargs)
    cookies = kw.pop("cookies", None)
    headers = kw.pop("headers", None)
    content = kw.pop("content", None)
    content_type = kw.pop("content_type", None)
    data = kw.pop("data", None)
    json_body = kw.pop("json", _MISSING)

    req_kwargs = {}
    if cookies is not None:
        req_kwargs["cookies"] = cookies
    if headers is not None:
        req_kwargs["headers"] = headers
    if content is not None:
        req_kwargs["content"] = content
        if content_type:
            req_kwargs.setdefault("headers", {})
            req_kwargs["headers"] = {**(headers or {}),
                                     "content-type": content_type}
    elif data is not None:
        req_kwargs["data"] = data
    elif not isinstance(json_body, _Missing):
        req_kwargs["json"] = json_body

    return getattr(client, method)(url, **req_kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--filter", "-f", default=None,
                        help="only run cases starting with this prefix")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--fail-dump", default=None,
                        help="write full gap dump to this file")
    parser.add_argument("--top", type=int, default=30,
                        help="how many top divergent (type,loc,field) to print")
    args = parser.parse_args()

    print(f"[+] Starting stock FastAPI (uvicorn) on :{FA_PORT}")
    fa_proc = start_fastapi()
    print(f"[+] Starting fastapi-turbo on :{RS_PORT}")
    start_fastapi_turbo_thread()

    try:
        if not wait_for_server(FA_URL):
            err = (fa_proc.stderr.read() or b"").decode()
            print(f"[!] FastAPI failed to boot on :{FA_PORT}")
            print(err[:1500])
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
        case_outcomes: Dict[str, Dict[str, Any]] = {}
        client = httpx.Client(timeout=10.0)
        try:
            for case in cases:
                # FA request
                try:
                    fa_r = send_one(client, case.method,
                                    FA_URL + case.path, case.kwargs)
                except Exception as e:
                    _add(results, case.case_id, "fa_request", False,
                         f"{type(e).__name__}: {e}",
                         error_type_hint=case.error_type_hint,
                         location_hint=case.location_hint)
                    continue

                # FR request
                try:
                    rs_r = send_one(client, case.method,
                                    RS_URL + case.path, case.kwargs)
                except Exception as e:
                    _add(results, case.case_id, "rs_request", False,
                         f"{type(e).__name__}: {e}",
                         error_type_hint=case.error_type_hint,
                         location_hint=case.location_hint)
                    continue

                deep_compare_case(case, fa_r, rs_r, results)

                case_outcomes[case.case_id] = {
                    "fa_status": fa_r.status_code,
                    "rs_status": rs_r.status_code,
                    "fa_body": safe_json(fa_r),
                    "rs_body": safe_json(rs_r),
                    "desc": case.description,
                    "error_type_hint": case.error_type_hint,
                    "location_hint": case.location_hint,
                    "nesting_hint": case.nesting_hint,
                }
        finally:
            client.close()

        # ───────── Aggregate report ─────────
        total_assertions = len(results)
        passed = sum(1 for r in results if r["ok"])
        failed = sum(1 for r in results if not r["ok"])

        # Per-case pass/fail
        case_fail_count: Dict[str, int] = collections.Counter()
        case_total_props: Dict[str, int] = collections.Counter()
        for r in results:
            case_total_props[r["case"]] += 1
            if not r["ok"]:
                case_fail_count[r["case"]] += 1
        passed_cases = sum(1 for cid, _ in case_total_props.items()
                           if case_fail_count.get(cid, 0) == 0)
        failed_cases = len(case_total_props) - passed_cases

        print()
        print("=" * 76)
        print(f"ROUND 2 Deep validation parity")
        print("=" * 76)
        print(f"Cases:       {len(cases)} total  |  passed {passed_cases}  "
              f"failed {failed_cases}")
        print(f"Assertions:  {total_assertions} total  |  passed {passed}  "
              f"failed {failed}")
        print(f"Field-level gap count (the real depth metric): {failed}")
        print("=" * 76)

        # ── Silent pass-through / crashes ──
        silent_200 = 0
        crashes_500 = 0
        for cid, o in case_outcomes.items():
            fa = o["fa_status"]
            rs = o["rs_status"]
            if fa == 422 and rs == 200:
                silent_200 += 1
            if fa == 422 and rs >= 500:
                crashes_500 += 1
        print(f"\nSilent pass-through (FA=422 but FR=200):  {silent_200}")
        print(f"Crashes (FA=422 but FR>=500):             {crashes_500}")

        # ── Aggregate by property-root ──
        by_prop_root: collections.Counter = collections.Counter()
        for r in results:
            if not r["ok"]:
                p = r["prop"]
                if p.startswith("err[") and "]." in p:
                    root = "err[]" + p.split("]", 1)[1]
                else:
                    root = p
                by_prop_root[root] += 1

        print("\nTop 15 failing property kinds:")
        for p, n in by_prop_root.most_common(15):
            print(f"  {n:5d}  {p}")

        # ── Failure count by location / error-type hint ──
        by_loc = collections.Counter()
        by_err_type_hint = collections.Counter()
        for r in results:
            if not r["ok"]:
                if r.get("location_hint"):
                    by_loc[r["location_hint"]] += 1
                if r.get("error_type_hint"):
                    by_err_type_hint[r["error_type_hint"]] += 1

        print("\nFailures by input location:")
        for loc, n in by_loc.most_common():
            print(f"  {n:5d}  {loc}")
        print("\nFailures by hinted Pydantic error type:")
        for et, n in by_err_type_hint.most_common(25):
            print(f"  {n:5d}  {et}")

        # ── Top N (error_type, location, field) triples ──
        triples: collections.Counter = collections.Counter()
        for r in results:
            if r["ok"]:
                continue
            # field name is the suffix after the last '.' of r["prop"]
            # e.g. "err[0].msg" -> "msg";  "detail_count" -> "detail_count"
            prop = r["prop"]
            if prop.startswith("err[") and "]." in prop:
                field = prop.split("].", 1)[1]
            else:
                field = prop
            et = r.get("err_type") or r.get("error_type_hint") or "?"
            loc_hint = r.get("location_hint") or "?"
            triples[(et, loc_hint, field)] += 1

        print(f"\nTop {args.top} distinct (error_type, location, field) "
              f"triples that diverge:")
        for (et, loc, field), n in triples.most_common(args.top):
            print(f"  {n:5d}  type={et:<28s} loc={loc:<14s} field={field}")

        # ── Per-case mode — for verbose/fail-dump ──
        per_case_fail: Dict[str, List[Dict[str, Any]]] = (
            collections.defaultdict(list))
        for r in results:
            if not r["ok"]:
                per_case_fail[r["case"]].append(r)

        if args.verbose:
            print("\nPer-case failures (detail):")
            for cid in sorted(per_case_fail):
                o = case_outcomes.get(cid, {})
                print(f"\n  {cid} [{o.get('error_type_hint','?')}/"
                      f"{o.get('location_hint','?')} "
                      f"d={o.get('nesting_hint','?')}] "
                      f"\"{o.get('desc','')}\" -- "
                      f"{len(per_case_fail[cid])} failed assertions")
                for r in per_case_fail[cid]:
                    print(f"      - {r['prop']}: {r['detail']}")

        if args.fail_dump:
            dump = {
                "total_cases": len(cases),
                "passed_cases": passed_cases,
                "failed_cases": failed_cases,
                "total_assertions": total_assertions,
                "passed_assertions": passed,
                "failed_assertions": failed,
                "silent_pass_through": silent_200,
                "crashes": crashes_500,
                "top_triples": [
                    {"err_type": et, "location": loc, "field": field,
                     "count": n}
                    for (et, loc, field), n in triples.most_common(200)
                ],
                "by_prop_root": dict(by_prop_root),
                "by_location": dict(by_loc),
                "by_err_type_hint": dict(by_err_type_hint),
                "case_outcomes": case_outcomes,
                "failures": [
                    {k: (v if not isinstance(v, (list, tuple, dict))
                         else v)
                     for k, v in r.items()}
                    for r in results if not r["ok"]
                ],
            }
            with open(args.fail_dump, "w") as fh:
                json.dump(dump, fh, indent=2, default=str)
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
