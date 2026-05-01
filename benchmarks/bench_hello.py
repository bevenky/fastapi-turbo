"""Benchmark fastapi-turbo vs baseline overhead measurement.

Drives requests with both the Rust-reqwest-backed
``fastapi_turbo.http.Client`` (the README-promoted client) AND
``httpx.Client`` so the table reflects what each client measures.
Earlier this script used only httpx, so the README's Rust-client
numbers were never reproduced from this file (R52 finding 3).
"""
import time
import statistics
import sys


def bench_requests(url, n=500, client_kind="turbo"):
    """Benchmark N sequential requests using the named client.

    ``client_kind`` ∈ {``"turbo"``, ``"httpx"``} — determines
    which library issues requests. ``"turbo"`` uses
    ``fastapi_turbo.http.Client`` so the numbers are directly
    comparable to the Rust-client claims in the README.
    """
    if client_kind == "turbo":
        import fastapi_turbo  # noqa: F401  # ensure shim installed
        from fastapi_turbo.http import Client as _C
        client = _C()
        get = client.get
        close = client.close
    elif client_kind == "httpx":
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

app.run(host="127.0.0.1", port=""" + str(port) + """)
""")
    
    proc = subprocess.Popen([sys.executable, "/tmp/bench_fastapi_turbo.py"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(2)
    
    print("=== fastapi-turbo Benchmark ===\n")

    for endpoint, label in (("/hello", "GET /hello (no deps, sync handler)"),
                            ("/with-deps", "GET /with-deps (2-level Depends chain, async)")):
        print(f"{label}:")
        for kind in ("turbo", "httpx"):
            stats = bench_requests(
                f"http://127.0.0.1:{port}{endpoint}", n=1000, client_kind=kind,
            )
            print(f"  [client={kind}]")
            for k, v in stats.items():
                print(f"    {k}: {v:.0f} μs")
        print()
    
    proc.kill()
    proc.wait()
