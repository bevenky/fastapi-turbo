"""Benchmark fastapi-turbo vs baseline overhead measurement."""
import time
import statistics
import sys

def bench_requests(url, n=500):
    """Benchmark N sequential requests."""
    import httpx
    client = httpx.Client()
    # Warmup
    for _ in range(10):
        client.get(url)
    
    latencies = []
    for _ in range(n):
        start = time.perf_counter_ns()
        resp = client.get(url)
        elapsed_us = (time.perf_counter_ns() - start) / 1000
        latencies.append(elapsed_us)
        assert resp.status_code == 200
    
    client.close()
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
from fastapi_turbo import FastAPI, Depends, Header
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
    
    print("GET /hello (no deps, sync handler):")
    stats = bench_requests(f"http://127.0.0.1:{port}/hello", n=1000)
    for k, v in stats.items():
        print(f"  {k}: {v:.0f} μs")
    
    print()
    
    print("GET /with-deps (2-level Depends chain, async):")
    stats = bench_requests(f"http://127.0.0.1:{port}/with-deps", n=1000)
    for k, v in stats.items():
        print(f"  {k}: {v:.0f} μs")
    
    proc.kill()
    proc.wait()
