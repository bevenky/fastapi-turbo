"""FAIR all-core throughput benchmark.

Every framework runs across ALL cores:
  turbo    -> FASTAPI_TURBO_WORKERS=N (multiprocess)
  uvicorn  -> --workers N
  fastify  -> CLUSTER=N (node cluster)
  gin      -> GOMAXPROCS default (all cores)
  axum     -> tokio multi_thread default (all cores)

Measures oha throughput at high concurrency AND total CPU% across the whole
process group (persistent psutil counters → correct multi-process accounting).
18-core box: 1800% = all cores fully used.
"""
from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time

import psutil

HOST = "127.0.0.1"
OHA = "/opt/homebrew/bin/oha"
HERE = os.path.dirname(os.path.abspath(__file__))
NCPU = psutil.cpu_count()
WORKERS = int(os.environ.get("BENCH_WORKERS", NCPU))
# Python ASYNC-heavy groups run at their measured-best worker count, not all
# cores: each worker pins one event loop to one GIL core, and past ~12 workers
# the extra processes only add thread thrash (audit: w18 is the WORST async
# config; w12 beats it by ~25%). CPU-bound groups keep all cores — same
# each-at-its-best policy as the driver choice.
ASYNC_WORKERS = int(os.environ.get("BENCH_ASYNC_WORKERS", str(min(WORKERS, 12))))
CONC = int(os.environ.get("BENCH_CONC", "100"))
DUR = os.environ.get("BENCH_DUR", "6s")

ITEM_BODY = '{"sku":"A","qty":3,"tags":["x","y"]}'
PATCH_BODY = '{"qty":9}'

ENDPOINTS = [
    ("GET hello sync", "GET", "/hello", None),
    ("GET hello async", "GET", "/async/hello", None),
    ("GET large JSON", "GET", "/json/large", None),
    ("GET large JSON async", "GET", "/async/json/large", None),
    ("POST items sync", "POST", "/items", ITEM_BODY),
    ("POST items async", "POST", "/async/items", ITEM_BODY),
    ("PUT items", "PUT", "/items/7", ITEM_BODY),
    ("PATCH items", "PATCH", "/items/7", PATCH_BODY),
    ("DELETE items", "DELETE", "/items/7", None),
    ("XML small", "GET", "/xml/small", None),
    ("XML large", "GET", "/xml/large", None),
    ("stream sync", "GET", "/stream-sync", None),
    ("stream async", "GET", "/stream-async", None),
    ("stream await", "GET", "/stream-await", None),
    ("redis GET sync", "GET", "/redis/get/sync", None),
    ("redis GET async", "GET", "/redis/get/async", None),
    ("redis SET sync", "POST", "/redis/set/sync", None),
    ("redis SET async", "POST", "/redis/set/async", None),
    ("pg item sync", "GET", "/pg/item/5/sync", None),
    ("pg item async", "GET", "/pg/item/5/async", None),
    ("pg list sync", "GET", "/pg/items/sync?limit=10", None),
    ("pg list async", "GET", "/pg/items/async?limit=10", None),
]


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.25)
        return s.connect_ex((HOST, port)) == 0


def wait_up(port, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        if port_open(port):
            return True
        time.sleep(0.2)
    return False


def kill_port(port):
    for pid in subprocess.run(["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True).stdout.split():
        try:
            os.kill(int(pid), signal.SIGKILL)
        except Exception:
            pass


def group_pids(pgid):
    pids = []
    for p in psutil.process_iter(["pid"]):
        try:
            if os.getpgid(p.pid) == pgid:
                pids.append(p.pid)
        except Exception:
            pass
    return pids


# Python frameworks bench DB/redis endpoint groups in ISOLATED boots so one
# backend's client pools (e.g. psycopg3-async worker tasks on the GIL-bound
# worker loop) can't degrade another's numbers — measured contamination was
# up to 4x on asyncpg. Go/Node/Rust have one native driver each; not needed.
PY_ISOLATE = {
    "redis": {"redis GET sync", "redis GET async", "redis SET sync", "redis SET async"},
    "pg-sync": {"pg item sync", "pg list sync"},
    "pg-async": {"pg item async", "pg list async"},
}
# Isolated groups dominated by Python async I/O boot at ASYNC_WORKERS.
ASYNC_GROUPS = {"redis", "pg-async"}


def registry():
    py = sys.executable
    return {
        "fastapi-turbo": dict(port=8902, cwd=HERE,
                              cmd=[py, "app.py", "8902"],
                              env={"BENCH_ENGINE": "turbo", "FASTAPI_TURBO_WORKERS": str(WORKERS)},
                              isolate=PY_ISOLATE),
        "FastAPI (uvicorn)": dict(port=8903, cwd=HERE,
                                  cmd=[py, "-m", "uvicorn", "app:app", "--host", HOST,
                                       "--port", "8903", "--workers", str(WORKERS), "--log-level", "warning"],
                                  env={}, isolate=PY_ISOLATE),
        "Gin (Go)": dict(port=8904, cwd=os.path.join(HERE, "go-gin"),
                         cmd=[os.path.join(HERE, "go-gin", "bench-gin")],
                         env={"PORT": "8904"}),
        "Fastify (Node)": dict(port=8905, cwd=os.path.join(HERE, "fastify"),
                               cmd=["node", "server.js"],
                               env={"PORT": "8905", "CLUSTER": str(WORKERS)}),
        "raw-axum": dict(port=8901, cwd=os.path.join(HERE, "raw-axum"),
                         cmd=[os.path.join(HERE, "raw-axum", "target", "release", "raw-axum-matrix")],
                         env={"PORT": "8901"}),
    }


def run_one(port, method, path, body, procmap):
    cmd = [OHA, "-c", str(CONC), "-z", DUR, "--no-tui", "-m", method]
    if body:
        cmd += ["-d", body, "-H", "content-type: application/json"]
    cmd.append(f"http://{HOST}:{port}{path}")
    oha = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    peak = 0.0
    time.sleep(0.8)
    while oha.poll() is None:
        tot = 0.0
        for pid, proc in list(procmap.items()):
            try:
                tot += proc.cpu_percent(interval=None)
            except Exception:
                procmap.pop(pid, None)
        peak = max(peak, tot)
        time.sleep(0.4)
    out = oha.communicate()[0]
    m = re.search(r"Requests/sec:\s*([\d.]+)", out)
    rps = float(m.group(1)) if m else 0.0
    return rps, peak


def _boot_and_bench(name, fw, endpoints, group, workers=WORKERS):
    port = fw["port"]
    kill_port(port)
    time.sleep(1)
    if port_open(port):
        print(f"!! {name}: port busy"); return {}
    env = dict(os.environ, **fw["env"])
    cmd = list(fw["cmd"])
    if workers != WORKERS:
        # per-group worker override (Python engines only)
        if "FASTAPI_TURBO_WORKERS" in env:
            env["FASTAPI_TURBO_WORKERS"] = str(workers)
        if "--workers" in cmd:
            cmd[cmd.index("--workers") + 1] = str(workers)
    proc = subprocess.Popen(cmd, cwd=fw["cwd"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            preexec_fn=os.setsid)
    try:
        if not wait_up(port):
            print(f"!! {name}: boot FAILED"); return {}
        time.sleep(3.0)  # let all workers spin up + pools warm
        pgid = os.getpgid(proc.pid)
        procmap = {}
        for pid in group_pids(pgid):
            try:
                p = psutil.Process(pid); p.cpu_percent(interval=None); procmap[pid] = p
            except Exception:
                pass
        print(f"== {name} [{group}]: {len(procmap)} procs, c={CONC}, workers={workers}")
        res = {}
        for label, method, path, body in endpoints:
            # refresh procmap (workers may have forked late)
            for pid in group_pids(pgid):
                if pid not in procmap:
                    try:
                        p = psutil.Process(pid); p.cpu_percent(interval=None); procmap[pid] = p
                    except Exception:
                        pass
            rps, peak = run_one(port, method, path, body, procmap)
            res[label] = {"rps": rps, "cpu_pct": peak, "cores": round(peak / 100, 1)}
            print(f"  {label:24s} {rps:11,.0f} rps   CPU={peak:6.0f}%  (~{peak/100:.1f}/{NCPU} cores)")
        return res
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass
        time.sleep(1.5)


def run_framework(name, fw):
    isolate = fw.get("isolate") or {}
    used = set()
    boots = []
    for gname, labels in isolate.items():
        eps = [e for e in ENDPOINTS if e[0] in labels]
        used |= {e[0] for e in eps}
        boots.append((gname, eps))
    main = [e for e in ENDPOINTS if e[0] not in used]
    boots.insert(0, ("main", main))
    res = {}
    for gname, eps in boots:
        if eps:
            w = ASYNC_WORKERS if gname in ASYNC_GROUPS else WORKERS
            res.update(_boot_and_bench(name, fw, eps, gname, workers=w))
    return res


def main():
    reg = registry()
    want = sys.argv[1:] or list(reg.keys())
    path = os.path.join(HERE, "results_throughput.json")
    # merge into existing results when re-running a subset of frameworks
    out = {}
    if os.path.exists(path):
        try:
            out = json.load(open(path)).get("data", {})
        except Exception:
            out = {}
    print(f"cores={NCPU} workers={WORKERS} async_workers={ASYNC_WORKERS} conc={CONC} dur={DUR}\n")
    for name in want:
        prev = out.get(name, {})
        fresh = run_framework(name, reg[name])
        out[name] = {**prev, **fresh}
    with open(path, "w") as f:
        json.dump({"meta": {"cores": NCPU, "workers": WORKERS, "async_workers": ASYNC_WORKERS,
                            "conc": CONC, "dur": DUR}, "data": out}, f, indent=2)
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
