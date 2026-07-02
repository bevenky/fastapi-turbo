"""Async-concurrency probe: does the server overlap concurrent async I/O waits?

/asleep/{ms}  — async handler: await asyncio.sleep(ms/1000)
/ssleep/{ms}  — sync handler:  time.sleep(ms/1000)   (CPython releases GIL during sleep)

At workers=1, concurrency=C, on a fixed sleep of T seconds:
  - a multiplexing event loop  → ~C/T rps   (all waits overlap on one loop)
  - no overlap (run-to-completion per request) → ~1/T rps  (×N threads for sync)
This isolates the async execution model from any driver/DB noise.
"""
from __future__ import annotations
import asyncio
import os
import time

if os.environ.get("BENCH_ENGINE") == "turbo":
    import fastapi_turbo  # noqa

from fastapi import FastAPI

app = FastAPI()


@app.get("/asleep/{ms}")
async def asleep(ms: int):
    await asyncio.sleep(ms / 1000)
    return {"ok": True}


@app.get("/ssleep/{ms}")
def ssleep(ms: int):
    time.sleep(ms / 1000)
    return {"ok": True}


@app.get("/_health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    if os.environ.get("BENCH_ENGINE") == "turbo":
        app.run(host="127.0.0.1", port=port, workers=int(os.environ.get("FASTAPI_TURBO_WORKERS", "1")))
    else:
        import uvicorn
        uvicorn.run("app_async_probe:app", host="127.0.0.1", port=port,
                    workers=int(os.environ.get("BENCH_WORKERS", "1")), log_level="warning")
