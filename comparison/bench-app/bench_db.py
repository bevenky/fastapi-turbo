#!/usr/bin/env python3
"""Database benchmark runner -- compares fastapi-rs vs Go Gin with real DB.

Starts both servers, runs the Rust bench client against each endpoint,
measures cold/warm cache performance, and prints comparison tables.

Usage:
    python3 bench_db.py [--requests N] [--warmup W] [--concurrent C]
"""

import subprocess
import sys
import os
import time
import signal
import socket
import json
import re
import argparse
import threading
from pathlib import Path

# ── Configuration ───────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent.parent
BENCH_CLIENT = PROJECT_ROOT / "target" / "release" / "fastapi-rs-bench"

PORT_FASTAPI_RS = 19030
PORT_GO_GIN = 19031

FRAMEWORKS = {
    "fastapi-rs": PORT_FASTAPI_RS,
    "Go-Gin": PORT_GO_GIN,
}

# ANSI colors
CYAN = "\033[0;36m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
RED = "\033[0;31m"
NC = "\033[0m"


# ── Process management ──────────────────────────────────────────────────

_processes = []


def cleanup():
    """Kill all spawned server processes."""
    print(f"\n{YELLOW}Cleaning up...{NC}")
    for proc in _processes:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    print(f"{GREEN}All servers stopped.{NC}")


def wait_for_port(port, name, timeout=20):
    """Wait for a server to become ready on the given port."""
    sys.stdout.write(f"  Waiting for {name} on :{port} ")
    sys.stdout.flush()
    for i in range(timeout):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=1)
            s.settimeout(1)
            s.sendall(f"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode())
            resp = s.recv(4096)
            s.close()
            if b"200" in resp and b"ok" in resp:
                print(f" {GREEN}ready{NC}")
                return True
        except Exception:
            pass
        sys.stdout.write(".")
        sys.stdout.flush()
        time.sleep(0.5)
    print(f" {RED}TIMEOUT{NC}")
    return False


def flush_redis():
    """Flush Redis cache keys used by the benchmark."""
    try:
        import redis
        r = redis.Redis()
        # Delete product cache keys
        for key in r.scan_iter("product:*"):
            r.delete(key)
        r.close()
    except Exception as e:
        # Fall back to redis-cli
        subprocess.run(["redis-cli", "EVAL", "for _,k in ipairs(redis.call('keys','product:*')) do redis.call('del',k) end", "0"],
                       capture_output=True)


def clean_test_products():
    """Remove products created during benchmark."""
    try:
        subprocess.run(
            ["psql", "-h", "localhost", "-U", "venky", "-d", "fastapi_rs_bench",
             "-c", "DELETE FROM products WHERE name LIKE 'BenchProduct%';"],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass


# ── Benchmark runners ───────────────────────────────────────────────────

def run_bench(port, path, n, warmup, method="GET", body="", content_type="application/json"):
    """Run the Rust bench client and return raw output."""
    cmd = [str(BENCH_CLIENT), "127.0.0.1", str(port), path, str(n), str(warmup)]
    if method != "GET":
        cmd.extend([method, body, content_type])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
    except Exception as e:
        return f"ERROR: {e}"


def run_bench_concurrent(port, path, n_total, warmup, num_threads=10, method="GET", body="", content_type="application/json"):
    """Run benchmark with concurrent connections using threading.

    Each thread opens its own TCP connection and runs n_total/num_threads requests.
    """
    n_per_thread = n_total // num_threads
    results = [None] * num_threads

    def worker(thread_id):
        try:
            cmd = [str(BENCH_CLIENT), "127.0.0.1", str(port), path,
                   str(n_per_thread), str(warmup)]
            if method != "GET":
                cmd.extend([method, body, content_type])
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            results[thread_id] = result.stdout.strip()
        except Exception as e:
            results[thread_id] = f"ERROR: {e}"

    threads = []
    t0 = time.perf_counter()
    for i in range(num_threads):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    elapsed = time.perf_counter() - t0
    total_rps = n_total / elapsed

    # Aggregate latencies from all threads
    p50s, p99s = [], []
    for r in results:
        if r and "p50=" in r:
            m = re.search(r"p50=(\d+)", r)
            if m:
                p50s.append(int(m.group(1)))
            m = re.search(r"p99=(\d+)", r)
            if m:
                p99s.append(int(m.group(1)))

    if p50s:
        avg_p50 = sum(p50s) // len(p50s)
        max_p99 = max(p99s) if p99s else 0
        return f"  concurrent({num_threads}) p50={avg_p50}us p99={max_p99}us | {total_rps:.0f} req/s"
    return f"  concurrent({num_threads}) | {total_rps:.0f} req/s (aggregate)"


# ── Result parsing ──────────────────────────────────────────────────────

def parse_rps(output):
    """Extract req/s from bench output."""
    m = re.search(r"(\d+)\s*(req|msg)/s", output)
    return int(m.group(1)) if m else None


def parse_p50(output):
    """Extract p50 latency (us) from bench output."""
    m = re.search(r"p50=(\d+)", output)
    return int(m.group(1)) if m else None


def parse_p99(output):
    """Extract p99 latency (us) from bench output."""
    m = re.search(r"p99=(\d+)", output)
    return int(m.group(1)) if m else None


# ── Table printing ──────────────────────────────────────────────────────

def print_table(title, tests, results, extractor, unit=""):
    """Print a markdown-formatted comparison table."""
    print(f"\n### {title}")
    print()

    fw_names = list(FRAMEWORKS.keys())
    header = f"| {'Endpoint':<32} |"
    for fw in fw_names:
        header += f" {fw:<14} |"
    print(header)

    sep = f"| {'-'*32} |"
    for _ in fw_names:
        sep += f" {'-'*14} |"
    print(sep)

    for test_key, label in tests:
        row = f"| {label:<32} |"
        for fw in fw_names:
            key = f"{fw},{test_key}"
            val = extractor(results.get(key, ""))
            row += f" {str(val or '?') + unit:<14} |"
        print(row)


# ── Main ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Database benchmark: fastapi-rs vs Go Gin")
    parser.add_argument("--requests", "-n", type=int, default=5000, help="Requests per test (default: 5000)")
    parser.add_argument("--warmup", "-w", type=int, default=200, help="Warmup requests (default: 200)")
    parser.add_argument("--concurrent", "-c", type=int, default=10, help="Concurrent connections for parallel tests (default: 10)")
    args = parser.parse_args()

    N = args.requests
    WARMUP = args.warmup
    CONCURRENT = args.concurrent

    signal.signal(signal.SIGINT, lambda s, f: (cleanup(), sys.exit(1)))

    print(f"{CYAN}{'='*60}{NC}")
    print(f"{CYAN}  Database Benchmark: fastapi-rs vs Go Gin{NC}")
    print(f"{CYAN}  PostgreSQL + Redis | {N} requests, {WARMUP} warmup{NC}")
    print(f"{CYAN}{'='*60}{NC}")
    print()

    # ── Step 1: Verify bench client ──
    print(f"{YELLOW}[1/4] Checking bench client...{NC}")
    if not BENCH_CLIENT.exists():
        print(f"  {RED}Bench client not found at {BENCH_CLIENT}{NC}")
        print(f"  Building with: cargo build --release --bin fastapi-rs-bench")
        subprocess.run(["cargo", "build", "--release", "--bin", "fastapi-rs-bench"],
                       cwd=str(PROJECT_ROOT), check=True)
    print(f"  {GREEN}Bench client ready{NC}")

    # ── Step 2: Build Go binary (skip if already present) ──
    print(f"{YELLOW}[2/4] Checking Go Gin DB server...{NC}")
    go_bin = SCRIPT_DIR / "db-gin"
    go_src = SCRIPT_DIR / "db_go_gin.go"
    if not go_bin.exists() or go_src.stat().st_mtime > go_bin.stat().st_mtime:
        subprocess.run(
            ["go", "build", "-o", "db-gin", "db_go_gin.go"],
            cwd=str(SCRIPT_DIR), check=True,
        )
        print(f"  {GREEN}Built db-gin binary{NC}")
    else:
        print(f"  {GREEN}db-gin binary up to date{NC}")

    # ── Step 3: Start servers ──
    print(f"{YELLOW}[3/4] Starting servers...{NC}")

    # fastapi-rs
    env_rs = os.environ.copy()
    env_rs["FASTAPI_RS_NO_SHIM"] = "1"
    proc_rs = subprocess.Popen(
        [sys.executable, str(SCRIPT_DIR / "db_fastapi_rs_app.py")],
        cwd=str(PROJECT_ROOT),
        env=env_rs,
        stdout=open(os.devnull, "w"),
        stderr=open(os.devnull, "w"),
    )
    _processes.append(proc_rs)

    # Go Gin
    env_go = os.environ.copy()
    env_go["PORT"] = str(PORT_GO_GIN)
    go_log = open("/tmp/bench_db_go.log", "w")
    proc_go = subprocess.Popen(
        [str(SCRIPT_DIR / "db-gin")],
        env=env_go,
        stdout=go_log,
        stderr=go_log,
    )
    _processes.append(proc_go)

    if not wait_for_port(PORT_FASTAPI_RS, "fastapi-rs"):
        print(f"{RED}Failed to start fastapi-rs server{NC}")
        cleanup()
        sys.exit(1)
    if not wait_for_port(PORT_GO_GIN, "Go Gin"):
        print(f"{RED}Failed to start Go Gin server{NC}")
        cleanup()
        sys.exit(1)

    # ── Step 4: Warm up connections (trigger lazy pool init) ──
    print(f"\n{YELLOW}[4/4] Warming up database connections...{NC}")
    for name, port in FRAMEWORKS.items():
        subprocess.run(
            ["curl", "-sf", f"http://127.0.0.1:{port}/products/1"],
            capture_output=True, timeout=10,
        )
    print(f"  {GREEN}Connection pools initialized{NC}")
    time.sleep(1)

    # ── Run benchmarks ──
    results = {}
    CREATE_BODY = json.dumps({"name": "BenchProduct", "price": 42.99, "category_id": 1, "stock": 10})

    tests = [
        ("health",       "GET",  "/health",               None),
        ("get_product",  "GET",  "/products/1",           None),
        ("list",         "GET",  "/products?limit=10&offset=0", None),
        ("create",       "POST", "/products",             CREATE_BODY),
        ("stats",        "GET",  "/categories/stats",     None),
        ("order",        "GET",  "/orders/1",             None),
    ]

    for fw_name, port in FRAMEWORKS.items():
        print(f"\n{CYAN}=== {fw_name} (port {port}) ==={NC}")

        for test_key, method, path, body in tests:
            label_map = {
                "health": "GET /health",
                "get_product": "GET /products/1 (JOIN)",
                "list": "GET /products?limit=10",
                "create": "POST /products (INSERT)",
                "stats": "GET /categories/stats (GROUP BY)",
                "order": "GET /orders/1 (multi-JOIN)",
            }
            label = label_map.get(test_key, path)
            print(f"{YELLOW}{label} ({N} requests):{NC}")

            output = run_bench(port, path, N, WARMUP, method, body or "")
            results[f"{fw_name},{test_key}"] = output
            print(f"  {output}")

        # Clean up created products before cache tests
        clean_test_products()

        # ── Redis cache tests ──
        print(f"\n{YELLOW}Redis cache -- cold (first hit, cache miss):{NC}")
        flush_redis()
        output = run_bench(port, "/cached/products/1", N, 0)  # No warmup for cold cache
        results[f"{fw_name},cache_cold"] = output
        print(f"  {output}")

        print(f"{YELLOW}Redis cache -- warm (cache hits):{NC}")
        # Warmup fills the cache, then measure cached reads
        output = run_bench(port, "/cached/products/1", N, WARMUP)
        results[f"{fw_name},cache_warm"] = output
        print(f"  {output}")

        # ── Concurrent test ──
        print(f"\n{YELLOW}Concurrent ({CONCURRENT} connections) -- GET /products/1:{NC}")
        output = run_bench_concurrent(port, "/products/1", N, WARMUP // CONCURRENT, CONCURRENT)
        results[f"{fw_name},concurrent"] = output
        print(f"  {output}")

    # Final cleanup of test data
    clean_test_products()

    # ── Print comparison tables ──
    print(f"\n\n{CYAN}{'='*60}{NC}")
    print(f"{CYAN}  COMPARISON TABLES{NC}")
    print(f"{CYAN}{'='*60}{NC}")

    test_labels = [
        ("health", "GET /health"),
        ("get_product", "GET /products/1 (JOIN)"),
        ("list", "GET /products?limit=10"),
        ("create", "POST /products (INSERT)"),
        ("stats", "GET /categories/stats (GROUP BY)"),
        ("order", "GET /orders/1 (multi-JOIN)"),
        ("cache_cold", "Redis cache (cold)"),
        ("cache_warm", "Redis cache (warm)"),
        ("concurrent", f"Concurrent ({CONCURRENT} conn)"),
    ]

    print_table("Throughput (req/s) -- higher is better", test_labels, results, parse_rps)
    print_table("Latency p50 (us) -- lower is better", test_labels, results, parse_p50, "us")
    print_table("Latency p99 (us) -- lower is better", test_labels, results, parse_p99, "us")

    # ── Speedup summary ──
    print(f"\n### Speedup: Go-Gin / fastapi-rs")
    print()
    for test_key, label in test_labels:
        rs_rps = parse_rps(results.get(f"fastapi-rs,{test_key}", ""))
        go_rps = parse_rps(results.get(f"Go-Gin,{test_key}", ""))
        if rs_rps and go_rps:
            ratio = go_rps / rs_rps
            winner = "Go" if ratio > 1 else "fastapi-rs"
            ratio_display = f"{ratio:.2f}x" if ratio >= 1 else f"{1/ratio:.2f}x"
            print(f"  {label:<35} {ratio_display} ({winner} faster)")
        else:
            print(f"  {label:<35} N/A")

    print(f"\n{GREEN}Benchmark complete!{NC}")

    cleanup()


if __name__ == "__main__":
    main()
