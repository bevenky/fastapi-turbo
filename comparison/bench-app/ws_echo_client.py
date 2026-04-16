"""WebSocket echo benchmark client.

Measures per-round-trip latency for text and binary frames.
"""
import asyncio
import json
import os
import statistics
import sys
import time

try:
    import websockets
except ImportError:
    print("pip install websockets", file=sys.stderr)
    sys.exit(1)


async def bench(url: str, n: int, warmup: int, payload: bytes | str):
    times = []
    async with websockets.connect(url, ping_interval=None, max_size=None) as ws:
        # Warmup
        for _ in range(warmup):
            await ws.send(payload)
            await ws.recv()
        # Measure
        for _ in range(n):
            t0 = time.perf_counter()
            await ws.send(payload)
            await ws.recv()
            times.append((time.perf_counter() - t0) * 1e6)
    times.sort()
    p50 = times[len(times) // 2]
    p99 = times[int(len(times) * 0.99)]
    mn = min(times)
    return {"p50_us": round(p50), "p99_us": round(p99), "min_us": round(mn), "msg_s": int(1e6 / p50)}


async def main():
    url = sys.argv[1]
    kind = sys.argv[2] if len(sys.argv) > 2 else "text"
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 5000
    warmup = int(sys.argv[4]) if len(sys.argv) > 4 else 500

    if kind == "text":
        payload = "hello-world" * 3  # ~33 chars
    else:
        # Binary with non-UTF8 bytes, like an audio packet
        payload = bytes([i % 256 for i in range(320)])  # 320 bytes = 20ms @ 8kHz 16-bit mono

    result = await bench(url, n, warmup, payload)
    result["kind"] = kind
    result["n"] = n
    print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
