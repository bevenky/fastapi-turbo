"""SQLAlchemy conn=1 latency sidecar (turbo vs uvicorn).

The CH-05 SQLAlchemy table is c64 throughput only; this records the latency
story: conn=1 p50 for the /sqla/*/select_one variants plus the raw-driver
reference rows, BOTH Python engines, single worker (w1 — conn=1 latency never
benefits from more workers and w1 keeps the pool budget trivial).

Method (same hygiene as run_db_matrix):
  - solo boots: ONE server process group alive per measurement, isolated per
    backend-driver group (sqla-sync3+core3 share one engine/pool → one boot;
    the two async ORM variants are different drivers → separate boots, which
    is stricter than the c64 matrix's shared pgm-sqla-async boot);
  - each endpoint primed + response-shape-checked before measuring;
  - medians of 3 runs x 12000 requests (1000 warmup each) via the Rust
    fastapi-turbo-bench client at conn=1.

Results merge FIELD-level into results_db.json: each existing row dict gains
a "conn1_p50_us" key (rps/cpu fields untouched); meta gains "conn1_lat".

Usage: python run_db_latency.py [fastapi-turbo] [FastAPI (uvicorn)]
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request

from bench_throughput import HERE, HOST, kill_port, port_open, wait_up

BENCH = os.path.join(HERE, "..", "..", "target", "release", "fastapi-turbo-bench")
REQS = int(os.environ.get("BENCH_LAT_REQS", "12000"))
WARM = int(os.environ.get("BENCH_LAT_WARMUP", "1000"))
RUNS = int(os.environ.get("BENCH_LAT_RUNS", "3"))

# boot-group → [(results_db.json row key, path)]; one boot per driver pool.
BOOTS = [
    ("sqla-sync", [("sqla sync3 select_one", "/sqla/sync3/select_one"),
                   ("sqla core3 select_one", "/sqla/core3/select_one")]),
    ("sqla-asyncpg", [("sqla asyncpg select_one", "/sqla/asyncpg/select_one")]),
    ("sqla-async3", [("sqla async3 select_one", "/sqla/async3/select_one")]),
    ("pgm-pg3sync", [("pg3sync select_one", "/pgm/pg3sync/select_one")]),
    ("pgm-asyncpg", [("asyncpg select_one", "/pgm/asyncpg/select_one")]),
]

ROW5 = {"id": 5, "sku": "SKU-5", "name": "item-5", "qty": 5}


def frameworks():
    py = sys.executable
    return {
        "fastapi-turbo": dict(port=8902,
                              cmd=[py, "app_db.py", "8902"],
                              env={"BENCH_ENGINE": "turbo", "FASTAPI_TURBO_WORKERS": "1"}),
        "FastAPI (uvicorn)": dict(port=8903,
                                  cmd=[py, "app_db.py", "8903"],
                                  env={"BENCH_WORKERS": "1"}),
    }


def prime(port, path):
    with urllib.request.urlopen(f"http://{HOST}:{port}{path}", timeout=15) as r:
        got = json.loads(r.read())
    if got != ROW5:
        raise RuntimeError(f"{path}: wrong body {str(got)[:80]}")


def bench_p50(port, path):
    out = subprocess.run(
        [BENCH, HOST, str(port), path, "--requests", str(REQS),
         "--warmup", str(WARM), "--connections", "1", "--format", "json"],
        capture_output=True, text=True, timeout=300).stdout
    for line in out.splitlines():
        if line.startswith("{") and "p50_us" in line:
            return json.loads(line)["p50_us"]
    raise RuntimeError(f"no JSON report for {path}: {out[:200]}")


def run_fw(name, fw):
    res = {}
    port = fw["port"]
    for group, eps in BOOTS:
        kill_port(port); time.sleep(1)
        if port_open(port):
            print(f"!! {name} port {port} busy"); return res
        proc = subprocess.Popen(fw["cmd"], cwd=HERE, env=dict(os.environ, **fw["env"]),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                preexec_fn=os.setsid)
        try:
            if not wait_up(port):
                print(f"!! {name} [{group}] boot FAILED"); continue
            time.sleep(2.0)
            for key, path in eps:
                prime(port, path)  # builds the lazy pool + verifies the contract
                samples = [bench_p50(port, path) for _ in range(RUNS)]
                med = statistics.median(samples)
                res[key] = med
                print(f"  {name:18s} {key:26s} p50 {med:7.1f} µs   (runs: "
                      + ", ".join(f"{s:.1f}" for s in samples) + ")")
        finally:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except Exception:
                pass
            time.sleep(1.2)
    return res


def main():
    reg = frameworks()
    want = sys.argv[1:] or list(reg.keys())
    p = os.path.join(HERE, "results_db.json")
    doc = json.load(open(p))
    for name in want:
        print(f"== {name}: w1, conn=1, {RUNS}x{REQS} reqs (warmup {WARM})")
        for key, med in run_fw(name, reg[name]).items():
            doc["data"].setdefault(name, {}).setdefault(key, {})["conn1_p50_us"] = med
    doc["meta"]["conn1_lat"] = {"workers": 1, "runs": RUNS, "requests": REQS,
                                "warmup": WARM, "date": time.strftime("%Y-%m-%d")}
    with open(p, "w") as f:
        json.dump(doc, f, indent=2)
    print(f"\nmerged conn1_p50_us into {p}")

    # latency ladder (per-request tax, µs) — printed for the report note
    for name in want:
        d = doc["data"].get(name, {})
        g = lambda k: (d.get(k) or {}).get("conn1_p50_us")  # noqa: E731
        raw, core, orm = g("pg3sync select_one"), g("sqla core3 select_one"), g("sqla sync3 select_one")
        araw, aorm = g("asyncpg select_one"), g("sqla asyncpg select_one")
        a3 = g("sqla async3 select_one")
        if None in (raw, core, orm):
            continue
        print(f"\n{name} conn=1 ladder (select one):")
        print(f"  raw pg3sync {raw:.1f} → Core {core:.1f} (+{core - raw:.1f}µs) → ORM {orm:.1f} (+{orm - core:.1f}µs)")
        if None not in (araw, aorm):
            extra = f"; ORM psycopg3-async {a3:.1f}" if a3 is not None else ""
            print(f"  raw asyncpg {araw:.1f} → ORM asyncpg {aorm:.1f} (+{aorm - araw:.1f}µs){extra}")


if __name__ == "__main__":
    main()
