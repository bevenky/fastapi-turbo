"""Cross-framework correctness gate for the deep DB/Redis endpoints."""
from __future__ import annotations
import json, os, subprocess, sys, time, urllib.request
from run_db_matrix import registry
from bench_throughput import HOST, port_open, wait_up, kill_port

CHECKS = [
    ("GET", "/pg/select_one/sync", {"id": 5, "sku": "SKU-5", "name": "item-5", "qty": 5}),
    ("GET", "/pg/select_one/async", {"id": 5, "sku": "SKU-5", "name": "item-5", "qty": 5}),
    ("GET", "/pg/select_list/sync", "items10"),
    ("POST", "/pg/insert/sync?commit=true", {"ok": True, "committed": True}),
    ("POST", "/pg/insert/async?commit=false", {"ok": True, "committed": False}),
    ("POST", "/pg/update/sync?commit=false", {"ok": True, "committed": False}),
    ("POST", "/pg/delete/async?commit=true", {"ok": True, "committed": True}),
    ("GET", "/redis/sync/get", {"id": 1, "sku": "SKU-1", "name": "item-1", "qty": 1}),
    ("GET", "/redis/async/get", {"id": 1, "sku": "SKU-1", "name": "item-1", "qty": 1}),
    ("POST", "/redis/sync/set", {"ok": True}),
    ("POST", "/redis/async/set_durable", {"ok": True}),
    ("POST", "/redis/sync/pipeline", {"ok": True, "n": 10}),
    ("POST", "/redis/async/multi", {"ok": True, "n": 10}),
]
PY_ONLY = [
    ("GET", "/pgm/pg3sync/select_one", {"id": 5, "sku": "SKU-5", "name": "item-5", "qty": 5}),
    ("GET", "/pgm/asyncpg/select_one", {"id": 5, "sku": "SKU-5", "name": "item-5", "qty": 5}),
    ("POST", "/pgm/pg2sync/insert?commit=false", {"ok": True, "committed": False}),
    ("POST", "/pgm/pg3async/update?commit=false", {"ok": True, "committed": False}),
]


def fetch(port, method, path):
    req = urllib.request.Request(f"http://{HOST}:{port}{path}", method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def main():
    reg = registry()
    want = sys.argv[1:] or list(reg.keys())
    ok_all = True
    for name in want:
        fw = reg[name]
        port = fw["port"]
        kill_port(port); time.sleep(1)
        proc = subprocess.Popen(fw["cmd"], cwd=fw["cwd"], env=dict(os.environ, **fw["env"]),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=os.setsid)
        try:
            if not wait_up(port):
                print(f"!! {name} boot FAILED"); ok_all = False; continue
            time.sleep(2.5)
            checks = CHECKS + (PY_ONLY if fw["scope"] == "py" else [])
            fails = []
            for method, path, exp in checks:
                try:
                    got = fetch(port, method, path)
                    if exp == "items10":
                        good = isinstance(got, dict) and len(got.get("items", [])) == 10
                    else:
                        good = got == exp
                    if not good:
                        fails.append(f"    {method} {path}: got {str(got)[:70]}")
                except Exception as e:
                    fails.append(f"    {method} {path}: EXC {e}")
            if fails:
                ok_all = False
                print(f"✗ {name}: {len(fails)}/{len(checks)} mismatch")
                print("\n".join(fails))
            else:
                print(f"✓ {name}: all {len(checks)} DB/redis endpoints match")
        finally:
            try: os.killpg(os.getpgid(proc.pid), 9)
            except Exception: pass
            time.sleep(1)
    print("\n" + ("ALL DB PARITY-CLEAN" if ok_all else "DB PARITY FAILURES ABOVE"))


if __name__ == "__main__":
    main()
