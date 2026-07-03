"""WebSocket echo benchmark across all 5 frameworks (the /ws CONTRACT).

Contract (mirrored in app.py, go-gin, fastify, raw-axum): server accepts at
/ws, then loops receive-text -> echo the SAME text back verbatim; closes
cleanly on client close. Byte-identical echo across all 5 servers.

Two modes per framework:
  LATENCY:    1 connection, N (default 5000) sequential echo round trips of a
              64-byte text message; per-message perf_counter timing; first
              WARMUP (default 500) samples discarded. Reports p50/p99 us.
  THROUGHPUT: PROCS worker processes (multiprocessing, default 6) x CONNS
              connections each (default 8); every connection pipelines
              send/recv echo round trips (PIPELINE in-flight, default 16)
              for DUR seconds (default 5). Reports summed msgs/s.

CLIENT CEILING: the Python client (websockets library, uvloop when
importable) tops out around the high-100k msgs/s range on this class of
machine — server-side headroom beyond that is NOT visible. Its own event-loop
wakes also add ~25-30us to every observed round trip. Numbers are valid
for RELATIVE framework comparison, not as absolute server ceilings.

RUST CLIENT (ws-client-rs/): blocking tungstenite over a nodelay TcpStream —
no client-side scheduler jitter, so its latency numbers ARE the server RTT
(+ kernel). Each boot additionally runs `ws-bench lat` (3 repeats, median-p99
run published as rust_p50/p90/p99_us) and `ws-bench tp`. Build with:
  cd ws-client-rs && cargo build --release

Registry: reuses run_db_matrix.registry() boot commands/ports, swapping
app_db.py -> app.py for the two Python frameworks (only app.py carries /ws)
with FASTAPI_TURBO_WORKERS/BENCH_WORKERS=8 and CLUSTER=8 for fastify.

Usage: python bench_ws.py [framework ...]     -> writes results_ws.json
"""
from __future__ import annotations

import asyncio
import json
import multiprocessing as mp
import os
import subprocess
import sys
import time

from bench_throughput import HOST, HERE, kill_port, port_open, wait_up
from run_db_matrix import registry

SERVER_WORKERS = int(os.environ.get("BENCH_WS_SERVER_WORKERS", "8"))
LAT_N = int(os.environ.get("BENCH_WS_N", "5000"))
LAT_WARMUP = int(os.environ.get("BENCH_WS_WARMUP", "500"))
PROCS = int(os.environ.get("BENCH_WS_PROCS", "6"))
CONNS = int(os.environ.get("BENCH_WS_CONNS", "8"))
DUR = float(os.environ.get("BENCH_WS_DUR", "5"))
PIPELINE = int(os.environ.get("BENCH_WS_PIPELINE", "16"))

# Rust client (true-server-RTT latency + native-speed throughput).
WS_RS = os.path.join(HERE, "ws-client-rs", "target", "release", "ws-bench")
RS_N = int(os.environ.get("BENCH_WS_RS_N", "10000"))
RS_WARMUP = int(os.environ.get("BENCH_WS_RS_WARMUP", "1000"))
RS_REPEATS = int(os.environ.get("BENCH_WS_RS_REPEATS", "3"))
RS_TP_CONNS = int(os.environ.get("BENCH_WS_RS_TP_CONNS", str(PROCS * CONNS)))

MSG = "x" * 64  # 64-byte text message


def _run(coro):
    """asyncio.run, on uvloop when importable (faster client == less client
    bottleneck)."""
    try:
        import uvloop
        return uvloop.run(coro)
    except ImportError:
        return asyncio.run(coro)


def _connect(uri):
    import websockets
    # compression off (no permessage-deflate CPU tax), pings off (no
    # interference with tight RT timing).
    return websockets.connect(uri, compression=None, ping_interval=None)


def ws_registry():
    """run_db_matrix.registry() boots app_db.py for the Python frameworks;
    /ws lives in app.py (the canonical contract app) — swap the module and
    pin server-side workers."""
    reg = registry()
    for fw in reg.values():
        fw["cmd"] = ["app.py" if c == "app_db.py" else c for c in fw["cmd"]]
    reg["fastapi-turbo"]["env"]["FASTAPI_TURBO_WORKERS"] = str(SERVER_WORKERS)
    reg["FastAPI (uvicorn)"]["env"]["BENCH_WORKERS"] = str(SERVER_WORKERS)
    reg["Fastify (Node)"]["env"]["CLUSTER"] = str(SERVER_WORKERS)
    return reg


# ── LATENCY: 1 conn, sequential round trips ──────────────────────────
async def _latency(uri, n, warmup):
    samples = []
    async with _connect(uri) as ws:
        for i in range(n):
            t0 = time.perf_counter()
            await ws.send(MSG)
            echo = await ws.recv()
            dt = time.perf_counter() - t0
            if echo != MSG:
                raise RuntimeError(f"echo mismatch: {echo!r}")
            if i >= warmup:
                samples.append(dt * 1e6)
    samples.sort()
    return (samples[int(0.50 * (len(samples) - 1))],
            samples[int(0.99 * (len(samples) - 1))])


# ── THROUGHPUT: PROCS processes x CONNS pipelined connections ────────
async def _tp_conn(uri, dur, pipeline):
    count = 0
    async with _connect(uri) as ws:
        for _ in range(pipeline):          # prime the pipeline
            await ws.send(MSG)
        t0 = time.perf_counter()
        end = t0 + dur
        while time.perf_counter() < end:
            await ws.recv()
            count += 1
            await ws.send(MSG)
        for _ in range(pipeline):          # drain in-flight echoes
            await ws.recv()
            count += 1
        elapsed = time.perf_counter() - t0
    return count / elapsed


async def _tp_proc_main(uri, conns, dur, pipeline):
    rates = await asyncio.gather(*(_tp_conn(uri, dur, pipeline)
                                   for _ in range(conns)))
    return sum(rates)


def _tp_worker(uri, conns, dur, pipeline, out_q):
    """Top-level so it survives the spawn pickle (macOS default)."""
    try:
        out_q.put(_run(_tp_proc_main(uri, conns, dur, pipeline)))
    except Exception as e:  # noqa: BLE001 — report, don't hang the join
        out_q.put(e)


def throughput(uri):
    ctx = mp.get_context("spawn")
    out_q = ctx.Queue()
    procs = [ctx.Process(target=_tp_worker, args=(uri, CONNS, DUR, PIPELINE, out_q))
             for _ in range(PROCS)]
    for p in procs:
        p.start()
    total = 0.0
    for _ in procs:
        r = out_q.get(timeout=DUR + 60)
        if isinstance(r, Exception):
            raise r
        total += r
    for p in procs:
        p.join()
    return total


# ── Rust client (ws-client-rs): true server RTT, no Python-client noise ──
def rust_latency(uri):
    """3 repeats; publish the median-by-p99 run (background-load robust)."""
    runs = []
    for _ in range(RS_REPEATS):
        out = subprocess.run(
            [WS_RS, "lat", uri, "--n", str(RS_N), "--warmup", str(RS_WARMUP),
             "--json"],
            capture_output=True, text=True, check=True).stdout
        runs.append(json.loads(out))
    runs.sort(key=lambda r: r["p99_us"])
    return runs[len(runs) // 2], runs


def rust_throughput(uri):
    out = subprocess.run(
        [WS_RS, "tp", uri, "--conns", str(RS_TP_CONNS),
         "--pipeline", str(PIPELINE), "--dur", str(DUR), "--json"],
        capture_output=True, text=True, check=True).stdout
    return json.loads(out)["msgs_per_s"]


# ── smoke check: contract echo before measuring ──────────────────────
async def _smoke(uri):
    async with _connect(uri) as ws:
        await ws.send("hello-ws")
        echo = await ws.recv()
        assert echo == "hello-ws", f"echo mismatch: {echo!r}"


def run_fw(name, fw):
    port = fw["port"]
    uri = f"ws://{HOST}:{port}/ws"
    kill_port(port)
    time.sleep(1)
    if port_open(port):
        print(f"!! {name}: port busy")
        return None
    proc = subprocess.Popen(fw["cmd"], cwd=fw["cwd"], env=dict(os.environ, **fw["env"]),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            preexec_fn=os.setsid)
    try:
        if not wait_up(port):
            print(f"!! {name}: boot FAILED")
            return None
        time.sleep(3.0)  # let all workers spin up
        _run(_smoke(uri))
        p50, p99 = _run(_latency(uri, LAT_N, LAT_WARMUP))
        msgs = throughput(uri)
        res = {"p50_us": round(p50, 1), "p99_us": round(p99, 1),
               "msgs_per_s": round(msgs)}
        rs_note = ""
        if os.path.exists(WS_RS):
            med, runs = rust_latency(uri)
            res.update(rust_p50_us=med["p50_us"], rust_p90_us=med["p90_us"],
                       rust_p99_us=med["p99_us"],
                       rust_lat_runs=[{k: r[k] for k in
                                       ("p50_us", "p90_us", "p99_us")}
                                      for r in runs],
                       rust_msgs_per_s=round(rust_throughput(uri)))
            rs_note = (f"  | rust p50={med['p50_us']:6.1f} p99={med['p99_us']:6.1f}"
                       f"  {res['rust_msgs_per_s']:10,d} msgs/s")
        print(f"  {name:20s} p50={p50:8.1f}us  p99={p99:8.1f}us  "
              f"{msgs:12,.0f} msgs/s{rs_note}")
        return res
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except Exception:
            pass
        time.sleep(1.5)


def main():
    reg = ws_registry()
    want = sys.argv[1:] or list(reg.keys())
    path = os.path.join(HERE, "results_ws.json")
    out = {}
    if os.path.exists(path):  # merge when re-running a subset
        try:
            out = json.load(open(path)).get("data", {})
        except Exception:
            out = {}
    print(f"ws bench: lat n={LAT_N} (warmup {LAT_WARMUP}) | "
          f"tp {PROCS}p x {CONNS}c x {DUR}s pipeline={PIPELINE} | "
          f"server workers={SERVER_WORKERS}")
    if os.path.exists(WS_RS):
        print(f"rust client: lat n={RS_N} (warmup {RS_WARMUP}, {RS_REPEATS} "
              f"repeats, median-p99 run) | tp {RS_TP_CONNS}c "
              f"pipeline={PIPELINE} x {DUR}s\n")
    else:
        print(f"rust client MISSING ({WS_RS}) — Python-client numbers only\n")
    for name in want:
        res = run_fw(name, reg[name])
        if res is not None:
            out[name] = res
    with open(path, "w") as f:
        json.dump({"meta": {"lat_n": LAT_N, "lat_warmup": LAT_WARMUP,
                            "procs": PROCS, "conns": CONNS, "dur_s": DUR,
                            "pipeline": PIPELINE, "server_workers": SERVER_WORKERS,
                            "msg_bytes": len(MSG),
                            "rust_lat_n": RS_N, "rust_lat_warmup": RS_WARMUP,
                            "rust_lat_repeats": RS_REPEATS,
                            "rust_tp_conns": RS_TP_CONNS,
                            "note": "p50/p99_us + msgs_per_s = pure-Python "
                                    "client (websockets+uvloop): ~25-30us "
                                    "client-side overhead per RT, tops out "
                                    "~high-100k msgs/s — relative comparison "
                                    "only. rust_* = ws-client-rs blocking "
                                    "tungstenite: true server RTT."},
                   "data": out}, f, indent=2)
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
