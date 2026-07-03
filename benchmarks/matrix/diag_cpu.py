"""Diagnostic: does each framework/config actually use multiple cores under load?

Boots a server, runs oha at a given concurrency, and samples the TOTAL CPU%
of the server's whole process tree during the load (100% = 1 full core; on
this 18-core M5 Max, 1800% = all cores). Prints rps + peak/avg CPU%.
"""
from __future__ import annotations

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


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.25)
        return s.connect_ex((HOST, port)) == 0


def wait_up(port, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if port_open(port):
            return True
        time.sleep(0.2)
    return False


def tree_cpu(proc):
    """Sum cpu_percent across proc + all children (non-blocking, needs priming)."""
    procs = [proc] + proc.children(recursive=True)
    total = 0.0
    for p in procs:
        try:
            total += p.cpu_percent(interval=None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return total, len(procs)


def measure(label, boot_cmd, cwd, env_extra, port, path, conc, body=None, method="GET"):
    for p in (port,):
        for pid in subprocess.run(["lsof", "-ti", f"tcp:{p}"], capture_output=True, text=True).stdout.split():
            try:
                os.kill(int(pid), signal.SIGKILL)
            except Exception:
                pass
    time.sleep(1)
    env = dict(os.environ, PORT=str(port), **(env_extra or {}))
    proc = subprocess.Popen(boot_cmd, cwd=cwd, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, preexec_fn=os.setsid)
    try:
        if not wait_up(port):
            print(f"  {label:30s} BOOT FAILED")
            return
        time.sleep(2.0)
        ps = psutil.Process(proc.pid)
        tree_cpu(ps)  # prime cpu_percent counters
        for c in ps.children(recursive=True):
            try:
                c.cpu_percent(interval=None)
            except Exception:
                pass
        nproc = len(ps.children(recursive=True)) + 1

        cmd = [OHA, "-c", str(conc), "-z", "6s", "--no-tui", "-m", method,
               f"http://{HOST}:{port}{path}"]
        if body:
            cmd[-1:-1] = ["-d", body, "-H", "content-type: application/json"]
        oha = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        peak = 0.0
        samples = []
        time.sleep(1.0)
        while oha.poll() is None:
            cpu, n = tree_cpu(ps)
            peak = max(peak, cpu)
            samples.append(cpu)
            nproc = max(nproc, n)
            time.sleep(0.5)
        out = oha.communicate()[0]
        m = re.search(r"Requests/sec:\s*([\d.]+)", out)
        rps = float(m.group(1)) if m else 0.0
        avg = sum(samples) / len(samples) if samples else 0.0
        print(f"  {label:30s} c={conc:<3} {rps:11,.0f} rps   peakCPU={peak:6.0f}%  avgCPU={avg:6.0f}%  procs={nproc}  (~{peak/100:.1f} cores)")
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass
        time.sleep(1)


PY = sys.executable
if __name__ == "__main__":
    tests = sys.argv[1:] or ["all"]
    print(f"machine cores: {psutil.cpu_count()}")

    if "all" in tests or "turbo1" in tests:
        print("== turbo workers=1 ==")
        for path in ["/hello", "/json/large"]:
            measure("turbo w1", [PY, "app.py", "8910"], HERE, {"BENCH_ENGINE": "turbo", "FASTAPI_TURBO_WORKERS": "1"}, 8910, path, 32)

    if "all" in tests or "turbo8" in tests:
        print("== turbo workers=8 ==")
        for path in ["/hello", "/json/large"]:
            measure("turbo w8", [PY, "app.py", "8911"], HERE, {"BENCH_ENGINE": "turbo", "FASTAPI_TURBO_WORKERS": "8"}, 8911, path, 32)

    if "all" in tests or "turbo18" in tests:
        print("== turbo workers=18 ==")
        for path in ["/hello", "/json/large"]:
            measure("turbo w18", [PY, "app.py", "8912"], HERE, {"BENCH_ENGINE": "turbo", "FASTAPI_TURBO_WORKERS": "18"}, 8912, path, 64)

    if "all" in tests or "gin" in tests:
        print("== Gin (all cores by default) ==")
        for path in ["/hello", "/json/large"]:
            measure("gin", [os.path.join(HERE, "go-gin", "bench-gin")], os.path.join(HERE, "go-gin"), {}, 8904, path, 32)

    if "all" in tests or "axum" in tests:
        print("== raw-axum (all cores by default) ==")
        for path in ["/hello", "/json/large"]:
            measure("axum", [os.path.join(HERE, "raw-axum", "target", "release", "raw-axum-matrix")], os.path.join(HERE, "raw-axum"), {}, 8901, path, 32)
