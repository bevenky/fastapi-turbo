"""Deep Postgres + Redis benchmark runner.

- Cross-framework (all 5): /pg/{op}/{sync,async} + /redis/{mode}/{op}, writes x commit.
- Python driver matrix (turbo + uvicorn only): /pgm/{driver}/{op}, writes x commit.
- Redis durability: AOF is enabled ONCE per boot (not per row) and the bench
  gates on the initial background rewrite completing (aof_rewrite_in_progress=0)
  BEFORE flipping appendfsync=always; both set_durable rows then run
  back-to-back in that steady state and AOF is disabled after. The old
  per-row toggle benched DURING the CONFIG-SET-triggered rewrite fork
  (no-appendfsync-on-rewrite=no => fsyncs contend with the rewrite child;
  first run collapsed EVERY framework to 16-32 rps, later runs swung
  3.5k-9k on AOF state). set_durable measures Redis's synchronous-fsync
  group-commit floor (~66 rps per in-flight writer on this disk) — it scales
  with the client's in-flight command count; compare within a run only.
- bench_writes is TRUNCATE+reseeded before each framework (insert+commit grows it).
- Python sync boots run workers=8 (conn budget); async-heavy boots (async PG
  drivers, redis) run workers=12 — the measured-best async worker count.
  Per-boot pool isolation keeps every config under max_connections(100).

Usage: python run_db_matrix.py [framework ...]
       BENCH_GROUPS=redis python run_db_matrix.py        # re-run one row-group
       BENCH_GROUPS=pgm-pg2sync python run_db_matrix.py  # re-run one driver boot
       results merge ROW-level into results_db.json (other rows preserved).

Every row records oha's status-code distribution; >0.5% non-2xx marks the
row INVALID (err_pct in results). Guard added after pg2sync's 5-15k "rps"
turned out to be 55-64% HTTP 500s from psycopg2 pool-exhaustion raises.
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
# Python ASYNC-heavy boots (async PG drivers, redis) run at their measured-best
# worker count: one event loop per worker on one GIL core, and past ~12 workers
# extra processes only add thread thrash (w18 is the worst async config; w12
# beats it ~25%). Per-boot isolation means only ONE driver pool exists, so the
# conn budget stays tiny: 12 workers x max 2 = 24 << max_connections(100).
DB_ASYNC_WORKERS = int(os.environ.get("BENCH_DB_ASYNC_WORKERS", "12"))
ASYNC_BOOT_GROUPS = {"cross-pg-async", "redis", "pgm-pg3async", "pgm-asyncpg"}

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


def redis_cli_out(*args) -> str:
    return subprocess.run(["/opt/homebrew/bin/redis-cli", *args],
                          capture_output=True, text=True).stdout


def aof_prewarm(timeout=60.0):
    """Enable AOF once and gate on the fork'd initial rewrite completing.

    CONFIG SET appendonly yes triggers a background AOF rewrite (fork).
    Benching during it with appendfsync=always throttles wildly (fsyncs
    contend with the rewrite child's disk I/O and can block the main
    thread). Enable with everysec, WAIT for the rewrite to finish, kill
    auto-rewrites (no mid-bench forks), then flip to always.
    """
    redis_cli("CONFIG", "SET", "appendfsync", "everysec")
    redis_cli("CONFIG", "SET", "appendonly", "yes")
    redis_cli("CONFIG", "SET", "auto-aof-rewrite-percentage", "0")
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = redis_cli_out("INFO", "persistence")
        if ("aof_rewrite_in_progress:0" in info
                and "aof_rewrite_scheduled:0" in info
                and "aof_last_bgrewrite_status:ok" in info):
            break
        time.sleep(0.2)
    else:
        print("!! aof_prewarm: rewrite-complete gate timed out")
    redis_cli("CONFIG", "SET", "appendfsync", "always")
    time.sleep(0.3)


def aof_restore():
    redis_cli("CONFIG", "SET", "appendfsync", "everysec")
    redis_cli("CONFIG", "SET", "appendonly", "no")
    redis_cli("CONFIG", "SET", "auto-aof-rewrite-percentage", "100")


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
    m = re.search(r"Requests/sec:\s*([\d.]+)", out)
    # Non-2xx guard: pg2sync once reported 5-15k rps that was 55-64% HTTP 500
    # (psycopg2 pool exhaustion raises instead of blocking) — rps alone hides
    # error storms. Parse oha's status distribution and surface the share.
    codes = re.findall(r"\[(\d{3})\]\s+(\d+)\s+responses", out)
    total = sum(int(n) for _, n in codes)
    bad = sum(int(n) for c, n in codes if not c.startswith("2"))
    err_pct = round(100.0 * bad / total, 2) if total else 0.0
    return (float(m.group(1)) if m else 0.0), peak, err_pct


def _boots_for(fw):
    """Endpoint groups, each run in a FRESH server boot.

    Python frameworks get one boot per backend-driver group: pools are lazy
    (app_db._pools), so a boot that only hits one driver's endpoints creates
    ONLY that driver's pool — no co-resident pools sharing the GIL-bound
    worker loop. Measured contamination without this: asyncpg dropped 8.9k →
    3.5k rps when psycopg3-async's pool coexisted. Go/Node/Rust have one
    native driver — a single boot is already clean.
    """
    if fw["scope"] != "py":
        return [("all", list(CROSS))]
    is_pg = lambda e: e[1] in ("pg-read", "pg-write")  # noqa: E731
    boots = [
        ("cross-pg-sync", [e for e in CROSS if is_pg(e) and "[sync]" in e[0]]),
        ("cross-pg-async", [e for e in CROSS if is_pg(e) and "[async]" in e[0]]),
        ("redis", [e for e in CROSS if e[1] == "redis"]),
    ]
    for drv in ("pg3sync", "pg2sync", "pg3async", "asyncpg"):
        boots.append((f"pgm-{drv}", [e for e in PYMATRIX if e[0].startswith(drv)]))
    return boots


def run_fw(name, fw):
    port = fw["port"]
    res = {}
    only = {g for g in os.environ.get("BENCH_GROUPS", "").split(",") if g}
    for group, eps in _boots_for(fw):
        if only and group not in only:  # boot-group name (e.g. pgm-pg2sync) matches whole boot
            eps = [e for e in eps if e[1] in only]  # else filter by row group (e.g. redis)
        if not eps:
            continue
        kill_port(port); time.sleep(1)
        if port_open(port):
            print(f"!! {name} port busy"); return res
        reseed_writes()
        env = dict(os.environ, **fw["env"])
        workers = DBWORKERS
        if fw["scope"] == "py" and group in ASYNC_BOOT_GROUPS:
            workers = DB_ASYNC_WORKERS
            for k in ("FASTAPI_TURBO_WORKERS", "BENCH_WORKERS"):
                if k in fw["env"]:
                    env[k] = str(workers)
        proc = subprocess.Popen(fw["cmd"], cwd=fw["cwd"], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=os.setsid)
        try:
            if not wait_up(port):
                print(f"!! {name} [{group}] boot FAILED"); continue
            time.sleep(3.0)
            pgid = os.getpgid(proc.pid)
            procmap = {}
            for pid in group_pids(pgid):
                try:
                    p = psutil.Process(pid); p.cpu_percent(interval=None); procmap[pid] = p
                except Exception:
                    pass
            print(f"== {name} [{group}]: {len(procmap)} procs, {len(eps)} endpoints, c={CONC}, workers={workers}")

            def run_eps(subset):
                for label, grp, method, path in subset:
                    for pid in group_pids(pgid):
                        if pid not in procmap:
                            try:
                                p = psutil.Process(pid); p.cpu_percent(interval=None); procmap[pid] = p
                            except Exception:
                                pass
                    rps, cpu, err_pct = bench(port, method, path, procmap)
                    res[label] = {"rps": rps, "cpu_pct": cpu, "cores": round(cpu / 100, 1), "group": grp}
                    warn = ""
                    if err_pct > 0.5:
                        res[label]["err_pct"] = err_pct
                        warn = f"  !! {err_pct}% non-2xx — row is INVALID"
                    print(f"  {label:34s} {rps:10,.0f} rps  ({cpu/100:.1f}c){warn}")

            # durable rows LAST, back-to-back, in a pre-warmed steady AOF
            # state (rewrite complete, appendfsync=always, no auto-rewrites).
            run_eps([e for e in eps if "set_durable" not in e[3]])
            durable = [e for e in eps if "set_durable" in e[3]]
            if durable:
                aof_prewarm()
                try:
                    run_eps(durable)
                finally:
                    aof_restore()
        finally:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except Exception:
                pass
            time.sleep(1.5)
    return res


def main():
    reg = registry()
    want = sys.argv[1:] or list(reg.keys())
    p = os.path.join(HERE, "results_db.json")
    # merge into existing results when re-running a subset of frameworks
    out = {}
    if os.path.exists(p):
        try:
            out = json.load(open(p)).get("data", {})
        except Exception:
            out = {}
    print(f"cores={NCPU} db_workers={DBWORKERS} async_workers={DB_ASYNC_WORKERS} conc={CONC} dur={DUR}\n")
    for name in want:
        merged = dict(out.get(name, {}))
        merged.update(run_fw(name, reg[name]))
        out[name] = merged
    # restore redis
    aof_restore()
    with open(p, "w") as f:
        json.dump({"meta": {"cores": NCPU, "db_workers": DBWORKERS, "async_workers": DB_ASYNC_WORKERS,
                            "conc": CONC, "dur": DUR}, "data": out}, f, indent=2)
    print(f"\nwrote {p}")


if __name__ == "__main__":
    main()
