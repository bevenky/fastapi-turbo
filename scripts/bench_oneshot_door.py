"""Step 4 efficiency gate: in-process oneshot door vs the Python dispatcher.

Drives FastAPI.__call__ in-process via httpx.ASGITransport (no socket, so it
isolates the engine cost) with the oneshot-door flag OFF (Python dispatcher)
then ON (Rust engine via tower::Service::oneshot), and reports requests/sec +
µs/request for representative workloads. The door must be FASTER than the
dispatcher for the common cases, or it does not ship (owner's constraint).
"""

import asyncio
import os
import time

import fastapi_turbo  # noqa: F401  (compat shim)
import httpx
from fastapi import Depends, FastAPI
from pydantic import BaseModel


class Item(BaseModel):
    name: str
    qty: int = 1
    tags: list[str] = []


def make_app() -> FastAPI:
    app = FastAPI()

    @app.get("/tiny")
    def tiny():
        return {"x": 1}

    @app.get("/json")
    def json_medium():
        return {"items": [{"id": i, "name": f"n{i}", "ok": True} for i in range(20)]}

    @app.post("/items")
    def create(item: Item):
        return {"created": item.name, "qty": item.qty, "tags": item.tags}

    def common_dep(q: str = "x"):
        return {"q": q}

    @app.get("/deps")
    def with_deps(d: dict = Depends(common_dep)):
        return {"dep": d}

    return app


async def _bench_one(app, method, path, n, json=None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        for _ in range(50):  # warmup
            await c.request(method, path, json=json)
        t0 = time.perf_counter()
        for _ in range(n):
            r = await c.request(method, path, json=json)
            assert r.status_code == 200, (path, r.status_code)
        dt = time.perf_counter() - t0
    return n / dt, dt / n * 1e6


WORKLOADS = [
    ("tiny-JSON-GET", "GET", "/tiny", None),
    ("medium-JSON-GET", "GET", "/json", None),
    ("POST+Pydantic", "POST", "/items", {"name": "w", "qty": 3, "tags": ["a", "b"]}),
    ("GET+Depends", "GET", "/deps?q=hello", None),
]


async def _bench_concurrent(app, method, path, n, conc, json=None):
    """Throughput with `conc` requests in flight — the realistic server
    metric. The door offloads each request to a worker thread with the GIL
    released, so CPU-bound Rust work runs in parallel; the Python dispatcher
    is GIL-bound on the single asyncio loop."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        for _ in range(50):
            await c.request(method, path, json=json)
        sem = asyncio.Semaphore(conc)

        async def one():
            async with sem:
                r = await c.request(method, path, json=json)
                assert r.status_code == 200

        t0 = time.perf_counter()
        await asyncio.gather(*(one() for _ in range(n)))
        dt = time.perf_counter() - t0
    return n / dt


async def main():
    n = int(os.environ.get("BENCH_N", "3000"))
    conc = int(os.environ.get("BENCH_CONC", "32"))

    print(f"=== SEQUENTIAL latency — {n} reqs/workload, httpx.ASGITransport ===\n")
    print(f"{'workload':<20} {'dispatcher':>14} {'oneshot door':>16} {'speedup':>9}")
    print("-" * 62)
    for label, method, path, json in WORKLOADS:
        os.environ.pop("FASTAPI_TURBO_ONESHOT_DOOR", None)
        _, us_off = await _bench_one(make_app(), method, path, n, json)
        os.environ["FASTAPI_TURBO_ONESHOT_DOOR"] = "1"
        _, us_on = await _bench_one(make_app(), method, path, n, json)
        os.environ.pop("FASTAPI_TURBO_ONESHOT_DOOR", None)
        print(f"{label:<20} {us_off:>8.1f}µs/r {us_on:>10.1f}µs/r {us_off / us_on:>8.2f}x")

    print(f"\n=== CONCURRENT throughput — {n} reqs, {conc} in flight (server metric) ===\n")
    print(f"{'workload':<20} {'dispatcher':>14} {'oneshot door':>16} {'speedup':>9}")
    print("-" * 62)
    for label, method, path, json in WORKLOADS:
        os.environ.pop("FASTAPI_TURBO_ONESHOT_DOOR", None)
        rps_off = await _bench_concurrent(make_app(), method, path, n, conc, json)
        os.environ["FASTAPI_TURBO_ONESHOT_DOOR"] = "1"
        rps_on = await _bench_concurrent(make_app(), method, path, n, conc, json)
        os.environ.pop("FASTAPI_TURBO_ONESHOT_DOOR", None)
        print(f"{label:<20} {rps_off:>10.0f}r/s {rps_on:>12.0f}r/s {rps_on / rps_off:>8.2f}x")


if __name__ == "__main__":
    asyncio.run(main())
