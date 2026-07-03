"""Cross-framework benchmark matrix runner.

Solo-boots ONE server at a time (co-resident servers inflate latency
+3-5us per the perf audit), warms it, benches every endpoint at conn=1
(latency: fastapi-turbo-bench) and conn=8 (throughput: oha), kills it,
moves on. Emits results_matrix.json for the HTML generator.

Usage:  python run_matrix.py [framework ...]   (default: all)
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
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
BENCH_BIN = ROOT / "target" / "release" / "fastapi-turbo-bench"
OHA = "/opt/homebrew/bin/oha"

HOST = "127.0.0.1"
N_REQS = 10_000
N_WARMUP = 1_000
OHA_CONC = 8
OHA_DUR = "4s"

ITEM_BODY = '{"sku":"A","qty":3,"tags":["x","y"]}'
PATCH_BODY = '{"qty":9}'
JSON_CT = "application/json"

# ── endpoint matrix: (label, category, method, path, body, content_type) ──
ENDPOINTS = [
    ("ping",              "baseline",  "GET",    "/_ping",                  None,       None),
    ("GET hello (sync)",  "simple",    "GET",    "/hello",                  None,       None),
    ("GET hello (async)", "simple",    "GET",    "/async/hello",            None,       None),
    ("GET large JSON",    "json",      "GET",    "/json/large",             None,       None),
    ("GET large JSON async","json",    "GET",    "/async/json/large",       None,       None),
    ("POST items (sync)", "post",      "POST",   "/items",                  ITEM_BODY,  JSON_CT),
    ("POST items (async)","post",      "POST",   "/async/items",            ITEM_BODY,  JSON_CT),
    ("PUT items/{id}",    "put",       "PUT",    "/items/7",                ITEM_BODY,  JSON_CT),
    ("PATCH items/{id}",  "patch",     "PATCH",  "/items/7",                PATCH_BODY, JSON_CT),
    ("DELETE items/{id}", "delete",    "DELETE", "/items/7",                None,       None),
    ("GET XML small",     "xml",       "GET",    "/xml/small",              None,       None),
    ("GET XML large",     "xml",       "GET",    "/xml/large",              None,       None),
    ("stream sync",       "streaming", "GET",    "/stream-sync",            None,       None),
    ("stream async",      "streaming", "GET",    "/stream-async",           None,       None),
    ("stream await",      "streaming", "GET",    "/stream-await",           None,       None),
    ("redis GET sync",    "redis",     "GET",    "/redis/get/sync",         None,       None),
    ("redis GET async",   "redis",     "GET",    "/redis/get/async",        None,       None),
    ("redis SET sync",    "redis",     "POST",   "/redis/set/sync",         None,       None),
    ("redis SET async",   "redis",     "POST",   "/redis/set/async",        None,       None),
    ("pg item sync",      "postgres",  "GET",    "/pg/item/5/sync",         None,       None),
    ("pg item async",     "postgres",  "GET",    "/pg/item/5/async",        None,       None),
    ("pg list sync",      "postgres",  "GET",    "/pg/items/sync?limit=10", None,       None),
    ("pg list async",     "postgres",  "GET",    "/pg/items/async?limit=10",None,       None),
]


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.25)
        return s.connect_ex((HOST, port)) == 0


def wait_up(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open(port):
            return True
        time.sleep(0.2)
    return False


def boot(fw: dict, port: int):
    env = dict(os.environ, PORT=str(port), **fw.get("env", {}))
    cmd = fw["cmd"](port)
    proc = subprocess.Popen(
        cmd, cwd=fw["cwd"], env=env, shell=isinstance(cmd, str),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    return proc


def kill(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def endpoint_ok(port, method, path, body, ct):
    """Preflight: does this framework serve this endpoint with a 2xx?"""
    import urllib.request
    import urllib.error
    url = f"http://{HOST}:{port}{path}"
    data = body.encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    if body:
        req.add_header("content-type", ct or JSON_CT)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError:
        return False
    except Exception:
        return False


def bench_conn1(port, method, path, body, ct):
    """fastapi-turbo-bench HOST PORT PATH N WARMUP [METHOD] [BODY] [CT] → p50/p99/rps."""
    cmd = [str(BENCH_BIN), HOST, str(port), path, str(N_REQS), str(N_WARMUP), method]
    if body is not None:
        cmd += [body, ct or JSON_CT]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout
    except subprocess.TimeoutExpired:
        return {}
    p50 = re.search(r"p50=(\d+)", out)
    p99 = re.search(r"p99=(\d+)", out)
    rps = re.search(r"([\d.]+)\s*req/s", out)
    return {
        "p50_us": int(p50.group(1)) if p50 else None,
        "p99_us": int(p99.group(1)) if p99 else None,
        "rps_c1": float(rps.group(1)) if rps else None,
    }


def bench_oha(port, method, path, body, ct):
    url = f"http://{HOST}:{port}{path}"
    cmd = [OHA, "-c", str(OHA_CONC), "-z", OHA_DUR, "--no-tui", "-m", method]
    if body is not None:
        cmd += ["-d", body, "-H", f"content-type: {ct or JSON_CT}"]
    cmd.append(url)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    except subprocess.TimeoutExpired:
        return {}
    rps = re.search(r"Requests/sec:\s*([\d.]+)", out)
    return {"rps_c8": float(rps.group(1)) if rps else None}


def run_framework(name: str, fw: dict) -> dict:
    port = fw["port"]
    if _port_open(port):
        print(f"  !! port {port} busy; skipping {name}", file=sys.stderr)
        return {}
    print(f"== {name}: booting on :{port}")
    proc = boot(fw, port)
    try:
        if not wait_up(port):
            print(f"  !! {name} failed to boot", file=sys.stderr)
            return {}
        time.sleep(1.0)  # settle / pool warm
        results = {}
        for label, cat, method, path, body, ct in ENDPOINTS:
            if not _port_open(port):
                print(f"  !! {name} died before {label}", file=sys.stderr)
                break
            if not endpoint_ok(port, method, path, body, ct):
                results[label] = {"category": cat, "method": method, "path": path,
                                  "p50_us": None, "p99_us": None, "rps_c1": None, "rps_c8": None}
                print(f"  {label:24s} (not served — skipped)")
                continue
            c1 = bench_conn1(port, method, path, body, ct)
            c8 = bench_oha(port, method, path, body, ct)
            row = {**c1, **c8, "category": cat, "method": method, "path": path}
            results[label] = row
            print(f"  {label:24s} p50={row.get('p50_us')}us p99={row.get('p99_us')}us "
                  f"c8={row.get('rps_c8')}rps")
        return results
    finally:
        kill(proc)
        time.sleep(0.5)


def build_registry() -> dict:
    py = sys.executable
    return {
        "raw-axum": {
            "port": 8901, "cwd": str(HERE / "raw-axum"),
            "cmd": lambda p: str(HERE / "raw-axum" / "target" / "release" / "raw-axum-matrix"),
        },
        "fastapi-turbo": {
            "port": 8902, "cwd": str(HERE),
            "env": {"BENCH_ENGINE": "turbo"},
            "cmd": lambda p: [py, "app.py", str(p)],
        },
        "FastAPI (uvicorn)": {
            "port": 8903, "cwd": str(HERE),
            "cmd": lambda p: [py, "app.py", str(p)],
        },
        "Gin (Go)": {
            "port": 8904, "cwd": str(HERE / "go-gin"),
            "cmd": lambda p: str(HERE / "go-gin" / "bench-gin"),
        },
        "Fastify (Node)": {
            "port": 8905, "cwd": str(HERE / "fastify"),
            "cmd": lambda p: ["node", "server.js"],
        },
    }


def main():
    reg = build_registry()
    want = sys.argv[1:] or list(reg.keys())
    all_results = {}
    for name in want:
        if name not in reg:
            print(f"unknown framework {name!r}; known: {list(reg)}", file=sys.stderr)
            continue
        all_results[name] = run_framework(name, reg[name])
    out = HERE / "results_matrix.json"
    out.write_text(json.dumps(all_results, indent=2))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
