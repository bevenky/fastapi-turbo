"""Benchmark fastapi-turbo vs baseline overhead measurement.

Drives requests with ``httpx.Client``. The Rust-reqwest-backed
``fastapi_turbo.http.Client`` leg was removed when the ``http``
module was deleted during the clone-deletion rewrite; the compiled
``fastapi-turbo-bench`` client (``benchmarks/run_bench.py``) is now
the canonical low-overhead measurement path — httpx numbers here
include ~3x client-side overhead and are only useful relatively.
"""
import time
import statistics
import sys


def bench_requests(url, n=500, client_kind="httpx"):
    """Benchmark N sequential requests using the named client."""
    if client_kind == "httpx":
        import httpx
        client = httpx.Client()
        get = client.get
        close = client.close
    else:
        raise ValueError(f"unknown client_kind {client_kind!r}")
    # Warmup
    for _ in range(10):
        get(url)

    latencies = []
    for _ in range(n):
        start = time.perf_counter_ns()
        resp = get(url)
        elapsed_us = (time.perf_counter_ns() - start) / 1000
        latencies.append(elapsed_us)
        assert resp.status_code == 200

    close()
    latencies.sort()
    return {
        "p50": latencies[len(latencies) // 2],
        "p99": latencies[int(len(latencies) * 0.99)],
        "mean": statistics.mean(latencies),
        "min": min(latencies),
        "max": max(latencies),
    }

if __name__ == "__main__":
    import subprocess, socket, os
    
    port = 19876
    
    # Write test apps
    with open("/tmp/bench_fastapi_turbo.py", "w") as f:
        f.write("""
import fastapi_turbo
from fastapi import FastAPI, Depends, Header
app = FastAPI()

async def get_db():
    return {"connected": True}

async def get_user(db=Depends(get_db), authorization: str = Header("token")):
    return {"name": "alice"}

@app.get("/hello")
def hello():
    return {"message": "hello"}

@app.get("/with-deps")
async def with_deps(user=Depends(get_user), db=Depends(get_db)):
    return {"user": user["name"], "db": db["connected"]}

app.run(host="127.0.0.1", port=""" + str(port) + """, workers=1)
""")
    
    proc = subprocess.Popen([sys.executable, "/tmp/bench_fastapi_turbo.py"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(2)
    
    print("=== fastapi-turbo Benchmark ===\n")

    for endpoint, label in (("/hello", "GET /hello (no deps, sync handler)"),
                            ("/with-deps", "GET /with-deps (2-level Depends chain, async)")):
        print(f"{label}:")
        stats = bench_requests(
            f"http://127.0.0.1:{port}{endpoint}", n=1000, client_kind="httpx",
        )
        print("  [client=httpx]")
        for k, v in stats.items():
            print(f"    {k}: {v:.0f} μs")
        print()
    
    proc.kill()
    proc.wait()
