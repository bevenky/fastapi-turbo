"""Cross-framework correctness gate: boot each server solo, hit every
endpoint, and assert the response matches the canonical contract
(JSON parsed-equal; XML/stream byte-equal; sizes for large payloads).
Run BEFORE benchmarking — latency on wrong output is meaningless.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

from run_matrix import build_registry, boot, kill, wait_up, _port_open, HOST

ITEM = '{"sku":"A","qty":3,"tags":["x","y"]}'
PATCH = '{"qty":9}'

# (method, path, body, expected) — expected: ("json", obj) | ("bytes", b) | ("size", n) | ("text", s)
CHECKS = [
    ("GET", "/_ping", None, ("json", {"ping": "pong"})),
    ("GET", "/hello", None, ("json", {"message": "hello"})),
    ("GET", "/async/hello", None, ("json", {"message": "hello"})),
    ("GET", "/json/large", None, ("size", 28791)),
    ("GET", "/async/json/large", None, ("size", 28791)),
    ("POST", "/items", ITEM, ("json", {"ok": True, "sku": "A", "qty": 3, "tag_count": 2})),
    ("POST", "/async/items", ITEM, ("json", {"ok": True, "sku": "A", "qty": 3, "tag_count": 2})),
    ("PUT", "/items/7", ITEM, ("json", {"ok": True, "id": 7, "sku": "A", "qty": 3, "tag_count": 2})),
    ("PATCH", "/items/7", PATCH, ("json", {"ok": True, "id": 7, "qty": 9})),
    ("DELETE", "/items/7", None, ("json", {"ok": True, "deleted": 7})),
    ("GET", "/xml/small", None, ("text", '<?xml version="1.0" encoding="UTF-8"?><item><id>1</id><name>item-1</name></item>')),
    ("GET", "/xml/large", None, ("size", 45833)),
    ("GET", "/stream-sync", None, ("size", 640)),
    ("GET", "/stream-async", None, ("size", 640)),
    ("GET", "/stream-await", None, ("size", 640)),
    ("GET", "/redis/get/sync", None, ("json", {"id": 1, "sku": "SKU-1", "name": "item-1", "qty": 1})),
    ("GET", "/redis/get/async", None, ("json", {"id": 1, "sku": "SKU-1", "name": "item-1", "qty": 1})),
    ("POST", "/redis/set/sync", None, ("json", {"ok": True})),
    ("POST", "/redis/set/async", None, ("json", {"ok": True})),
    ("GET", "/pg/item/5/sync", None, ("json", {"id": 5, "sku": "SKU-5", "name": "item-5", "qty": 5})),
    ("GET", "/pg/item/5/async", None, ("json", {"id": 5, "sku": "SKU-5", "name": "item-5", "qty": 5})),
    ("GET", "/pg/items/sync?limit=2", None, ("json", {"items": [
        {"id": 1, "sku": "SKU-1", "name": "item-1", "qty": 1},
        {"id": 2, "sku": "SKU-2", "name": "item-2", "qty": 2}]})),
    ("GET", "/pg/items/async?limit=2", None, ("json", {"items": [
        {"id": 1, "sku": "SKU-1", "name": "item-1", "qty": 1},
        {"id": 2, "sku": "SKU-2", "name": "item-2", "qty": 2}]})),
]


def fetch(port, method, path, body):
    url = f"http://{HOST}:{port}{path}"
    data = body.encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    if body:
        req.add_header("content-type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read()


def check(port, method, path, body, expected):
    raw = fetch(port, method, path, body)
    kind, exp = expected
    if kind == "json":
        return json.loads(raw) == exp, f"got {raw[:80]!r}"
    if kind == "text":
        return raw.decode() == exp, f"got {raw[:90]!r}"
    if kind == "size":
        return len(raw) == exp, f"got {len(raw)} bytes (want {exp})"
    if kind == "bytes":
        return raw == exp, f"got {raw[:80]!r}"
    return False, "unknown check"


def main():
    reg = build_registry()
    want = sys.argv[1:] or list(reg.keys())
    overall_ok = True
    for name in want:
        fw = reg[name]
        port = fw["port"]
        if _port_open(port):
            print(f"!! {name}: port {port} busy, skip"); continue
        proc = boot(fw, port)
        try:
            if not wait_up(port):
                print(f"!! {name}: boot FAILED"); overall_ok = False; continue
            time.sleep(1.0)
            fails = []
            skipped = 0
            for method, path, body, expected in CHECKS:
                try:
                    ok, detail = check(port, method, path, body, expected)
                except urllib.error.HTTPError as e:
                    if path == "/_ping" and e.code == 404:
                        # documented asymmetry: only turbo serves the built-in
                        # Rust /_ping (same skip policy as run_matrix.py)
                        skipped += 1
                        continue
                    ok, detail = False, f"EXC {e}"
                except Exception as e:
                    ok, detail = False, f"EXC {e}"
                if not ok:
                    fails.append(f"    {method} {path}: {detail}")
            if fails:
                overall_ok = False
                print(f"✗ {name}: {len(fails)} mismatch")
                print("\n".join(fails))
            else:
                extra = f" ({skipped} not-served skipped)" if skipped else ""
                print(f"✓ {name}: all {len(CHECKS) - skipped} endpoints match contract{extra}")
        finally:
            kill(proc)
            time.sleep(0.5)
    print("\n" + ("ALL FRAMEWORKS PARITY-CLEAN" if overall_ok else "PARITY FAILURES ABOVE"))
    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
