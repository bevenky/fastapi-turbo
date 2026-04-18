"""fastapi-rs BackgroundTasks bench server.

Endpoints:
  POST /bg/sync    — schedule a sync no-op task
  POST /bg/write   — schedule a sync task that appends to a list
  POST /bg/cpu     — sync task that does 1000 hash iterations
  GET  /bg/count   — how many tasks have run
"""
import os
import sys
import hashlib
from fastapi_rs import FastAPI, BackgroundTasks
from fastapi_rs.responses import JSONResponse

app = FastAPI()
COUNT = [0]
LOG: list[str] = []


def noop():
    COUNT[0] += 1


def write_log(msg: str):
    LOG.append(msg)
    COUNT[0] += 1


def cpu_work():
    h = hashlib.sha256()
    for _ in range(1000):
        h.update(b"x")
    COUNT[0] += 1


@app.post("/bg/sync")
def bg_sync(bg: BackgroundTasks):
    bg.add_task(noop)
    return JSONResponse({"ok": True})


@app.post("/bg/write")
def bg_write(bg: BackgroundTasks):
    bg.add_task(write_log, "m")
    return JSONResponse({"ok": True})


@app.post("/bg/cpu")
def bg_cpu(bg: BackgroundTasks):
    bg.add_task(cpu_work)
    return JSONResponse({"ok": True})


@app.get("/bg/count")
def bg_count():
    return JSONResponse({"count": COUNT[0]})


@app.get("/health")
def health():
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8900
    app.run("127.0.0.1", port)
