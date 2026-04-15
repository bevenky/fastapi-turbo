#!/usr/bin/env python3
"""WebSocket benchmark: measures echo round-trip latency and throughput."""
import asyncio
import time
import sys
import websockets


async def bench_ws(url, n=5000, warmup=500, msg="hello websocket"):
    async with websockets.connect(url) as ws:
        # Warmup
        for _ in range(warmup):
            await ws.send(msg)
            await ws.recv()

        # Benchmark
        latencies = []
        start_total = time.perf_counter()
        for _ in range(n):
            s = time.perf_counter_ns()
            await ws.send(msg)
            resp = await ws.recv()
            latencies.append((time.perf_counter_ns() - s) / 1000)  # microseconds
        elapsed = time.perf_counter() - start_total

        latencies.sort()
        p50 = latencies[len(latencies) // 2]
        p99 = latencies[int(len(latencies) * 0.99)]
        rps = n / elapsed
        return {
            "p50": p50,
            "p99": p99,
            "min": latencies[0],
            "rps": rps,
        }


async def bench_throughput(url, n=10000, msg_size=1024):
    """Measure sustained throughput with larger messages."""
    msg = "x" * msg_size
    async with websockets.connect(url) as ws:
        # Small warmup
        for _ in range(200):
            await ws.send(msg)
            await ws.recv()

        start = time.perf_counter()
        for _ in range(n):
            await ws.send(msg)
            await ws.recv()
        elapsed = time.perf_counter() - start
        mb_per_sec = (n * msg_size * 2) / elapsed / 1024 / 1024
        return {
            "msg_per_sec": n / elapsed,
            "mb_per_sec": mb_per_sec,
        }


def print_table(results):
    """Print a formatted comparison table."""
    # Header
    print()
    print("=" * 90)
    print(f"{'Framework':<16} {'p50 (us)':>10} {'p99 (us)':>10} {'min (us)':>10} {'echo msg/s':>12} {'1KB msg/s':>12} {'MB/s':>8}")
    print("-" * 90)
    for name, data in results.items():
        lat = data.get("latency", {})
        thr = data.get("throughput", {})
        print(
            f"{name:<16} "
            f"{lat.get('p50', 0):>10.0f} "
            f"{lat.get('p99', 0):>10.0f} "
            f"{lat.get('min', 0):>10.0f} "
            f"{lat.get('rps', 0):>12.0f} "
            f"{thr.get('msg_per_sec', 0):>12.0f} "
            f"{thr.get('mb_per_sec', 0):>8.1f}"
        )
    print("=" * 90)
    print()


async def bench_one(name, url, n=5000):
    """Benchmark a single server."""
    print(f"  [{name}] Latency benchmark ({n} messages)...")
    lat = await bench_ws(url, n=n)
    print(f"    p50={lat['p50']:.0f}us  p99={lat['p99']:.0f}us  min={lat['min']:.0f}us  {lat['rps']:.0f} msg/s")

    print(f"  [{name}] Throughput benchmark (1KB messages)...")
    thr = await bench_throughput(url)
    print(f"    {thr['msg_per_sec']:.0f} msg/s  {thr['mb_per_sec']:.1f} MB/s")

    return {"latency": lat, "throughput": thr}


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5000

    servers = {
        "Jamun":    "ws://127.0.0.1:18005/ws",
        "Go Gin":   "ws://127.0.0.1:18002/ws",
        "Go Echo":  "ws://127.0.0.1:18003/ws",
        "Fastify":  "ws://127.0.0.1:18004/ws",
        "FastAPI":  "ws://127.0.0.1:18006/ws",
    }

    print(f"\nWebSocket Echo Benchmark  ({n} messages per test)")
    print("=" * 50)

    results = {}
    for name, url in servers.items():
        try:
            results[name] = await bench_one(name, url, n=n)
        except Exception as e:
            print(f"  [{name}] FAILED: {e}")

    print_table(results)


if __name__ == "__main__":
    asyncio.run(main())
