"""Deep Postgres + Redis benchmark runner.

- Cross-framework (all 5): /pg/{op}/{sync,async} + /redis/{mode}/{op}, writes x commit.
- Python driver matrix (turbo + uvicorn only): /pgm/{driver}/{op}, writes x commit.
- Redis durability: enables AOF (appendfsync=always) around the set_durable test,
  disables it after — so set_durable measures the real fsync cost vs set.
- bench_writes is TRUNCATE+reseeded before each framework (insert+commit grows it).
- Postgres workers=8 (not 18): 4 driver pools x 8 workers x max 2 = 64 < max_connections(100).

Usage: python run_db_matrix.py [framework ...]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

import psutil

from bench_throughput import (HOST, OHA, HERE, NCPU, port_open, wait_up,
                              kill_port, group_pids)

CONC = int(os.environ.get("BENCH_CONC", "64"))
DUR = os.environ.get("BENCH_DUR", "4s")
DBWORKERS = int(os.environ.get("BENCH_DB_WORKERS", "8"))

CT = ("true", "false")  # commit modes

# cross-framework endpoints (all 5). scope="all"
CROSS = []
for io in ("sync", "async"):
    CROSS.append((f"PG select_one [{io}]", "pg-read", "GET", f"/pg/select_one/{io}"))
    CROSS.append((f"PG select_list [{io}]", "pg-read", "GET", f"/pg/select_list/{io}"))
    for op in ("insert", "update", "delete"):
        for c in CT:
            CROSS.append((f"PG {op} commit={c} [{io}]", "pg-write", "POST", f"/pg/{op}/{io}?commit={c}"))
for mode in ("sync", "async"):
    for rop in ("get", "set", "set_durable", "pipeline", "multi"):
        m = "GET" if rop == "get" else "POST"
        CROSS.append((f"redis {rop} [{mode}]", "redis", m, f"/redis/{mode}/{rop}"))

# Python driver matrix (turbo + uvicorn). scope="py"
PYMATRIX = []
for drv in ("pg3sync", "pg2sync", "pg3async", "asyncpg"):
    PYMATRIX.append((f"{drv} select_one", "pgm", "GET", f"/pgm/{drv}/select_one"))
    PYMATRIX.append((f"{drv} select_list", "pgm", "GET", f"/pgm/{drv}/select_list"))
    for op in ("insert", "update", "delete"):
        for c in CT:
            PYMATRIX.append((f"{drv} {op} commit={c}", "pgm", "POST", f"/pgm/{drv}/{op}?commit={c}"))


def redis_cli(*args):
    subprocess.run(["/opt/homebrew/bin/redis-cli", *args], capture_output=True, text=True)


def reseed_writes():
    subprocess.run(["psql", "-d", "fastapi_turbo_bench", "-tAc",
                    "TRUNCATE bench_writes; INSERT INTO bench_writes (val) "
                    "SELECT 'val-'||g FROM generate_series(1,10000) g;"],
                   capture_output=True, text=True)


def registry():
    py = sys.executable
    return {
        "fastapi-turbo": dict(port=8902, cwd=HERE, scope="py",
                              cmd=[py, "app_db.py", "8902"],
                              env={"BENCH_ENGINE": "turbo", "FASTAPI_TURBO_WORKERS": str(DBWORKERS)}),
        "FastAPI (uvicorn)": dict(port=8903, cwd=HERE, scope="py",
                                  cmd=[py, "app_db.py", "8903"],
                                  env={"BENCH_WORKERS": str(DBWORKERS)}),
        "Gin (Go)": dict(port=8904, cwd=os.path.join(HERE, "go-gin"), scope="all",
                         cmd=[os.path.join(HERE, "go-gin", "bench-gin")], env={"PORT": "8904"}),
        "Fastify (Node)": dict(port=8905, cwd=os.path.join(HERE, "fastify"), scope="all",
                               cmd=["node", "server.js"], env={"PORT": "8905", "CLUSTER": str(DBWORKERS)}),
        "raw-axum": dict(port=8901, cwd=os.path.join(HERE, "raw-axum"), scope="all",
                         cmd=[os.path.join(HERE, "raw-axum", "target", "release", "raw-axum-matrix")],
                         env={"PORT": "8901"}),
    }


def bench(port, method, path, procmap):
    durable = "set_durable" in path
    if durable:
        redis_cli("CONFIG", "SET", "appendonly", "yes")
        redis_cli("CONFIG", "SET", "appendfsync", "always")
    cmd = [OHA, "-c", str(CONC), "-z", DUR, "--no-tui", "-m", method, f"http://{HOST}:{port}{path}"]
    oha = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    peak = 0.0
    time.sleep(0.6)
    while oha.poll() is None:
        tot = 0.0
        for pid, p in list(procmap.items()):
            try:
                tot += p.cpu_percent(interval=None)
            except Exception:
                procmap.pop(pid, None)
        peak = max(peak, tot)
        time.sleep(0.4)
    out = oha.communicate()[0]
    if durable:
        redis_cli("CONFIG", "SET", "appendfsync", "everysec")
        redis_cli("CONFIG", "SET", "appendonly", "no")
    m = re.search(r"Requests/sec:\s*([\d.]+)", out)
    return (float(m.group(1)) if m else 0.0), peak


def run_fw(name, fw):
    port = fw["port"]
    kill_port(port); time.sleep(1)
    if port_open(port):
        print(f"!! {name} port busy"); return {}
    reseed_writes()
    proc = subprocess.Popen(fw["cmd"], cwd=fw["cwd"], env=dict(os.environ, **fw["env"]),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=os.setsid)
    try:
        if not wait_up(port):
            print(f"!! {name} boot FAILED"); return {}
        time.sleep(3.0)
        pgid = os.getpgid(proc.pid)
        procmap = {}
        for pid in group_pids(pgid):
            try:
                p = psutil.Process(pid); p.cpu_percent(interval=None); procmap[pid] = p
            except Exception:
                pass
        eps = list(CROSS)
        if fw["scope"] == "py":
            eps += PYMATRIX
        print(f"== {name}: {len(procmap)} procs, {len(eps)} endpoints, c={CONC}")
        res = {}
        for label, grp, method, path in eps:
            for pid in group_pids(pgid):
                if pid not in procmap:
                    try:
                        p = psutil.Process(pid); p.cpu_percent(interval=None); procmap[pid] = p
                    except Exception:
                        pass
            rps, cpu = bench(port, method, path, procmap)
            res[label] = {"rps": rps, "cpu_pct": cpu, "cores": round(cpu / 100, 1), "group": grp}
            print(f"  {label:34s} {rps:10,.0f} rps  ({cpu/100:.1f}c)")
        return res
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except Exception:
            pass
        time.sleep(1.5)


def main():
    reg = registry()
    want = sys.argv[1:] or list(reg.keys())
    out = {}
    print(f"cores={NCPU} db_workers={DBWORKERS} conc={CONC} dur={DUR}\n")
    for name in want:
        out[name] = run_fw(name, reg[name])
    # restore redis
    redis_cli("CONFIG", "SET", "appendonly", "no")
    p = os.path.join(HERE, "results_db.json")
    with open(p, "w") as f:
        json.dump({"meta": {"cores": NCPU, "db_workers": DBWORKERS, "conc": CONC, "dur": DUR}, "data": out}, f, indent=2)
    print(f"\nwrote {p}")


if __name__ == "__main__":
    main()
