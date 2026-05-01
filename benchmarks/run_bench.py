"""FastAPI + uvicorn vs fastapi-turbo vs Go Gin head-to-head benchmark.

Boots all three servers from an **endpoint-identical** app (one FastAPI
module running under both uvicorn and fastapi-turbo, a matching Go Gin
binary under ``benchmarks/go-gin``) and runs the compiled Rust bench
client against each across representative endpoint shapes. Emits a
markdown-ready table for ``benchmarks.md``.

Run:
    python benchmarks/run_bench.py
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

BENCH_BIN = ROOT / "target" / "release" / "fastapi-turbo-bench"
APP_FILE = HERE / "_bench_app.py"
GIN_BIN = HERE / "go-gin" / "bench-gin"

HOST = "127.0.0.1"
N_REQS = 20_000
N_WARMUP = 3_000


# ── Shared parity app ────────────────────────────────────────────────
APP_SRC = '''"""Bench app — identical source runs under both uvicorn (FA) and
fastapi-turbo so every comparison is apples-to-apples."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, Depends, Header
from pydantic import BaseModel


app = FastAPI()


class Item(BaseModel):
    sku: str
    qty: int
    tags: list[str] = []


# ── simple JSON returning handlers ──
@app.get("/hello")
def hello():
    return {"message": "hello"}


@app.get("/path/{id}")
def path_param(id: int):
    return {"id": id, "squared": id * id}


@app.get("/headers")
def headers(user_agent: str = Header("unknown"), x_request_id: str = Header(None)):
    return {"ua": user_agent, "req_id": x_request_id}


# ── dependency injection ──
async def get_db() -> dict:
    return {"connected": True}


async def get_user(
    db: dict = Depends(get_db),
    authorization: str = Header("tok-demo"),
) -> dict:
    return {"name": "alice", "db": db["connected"]}


@app.get("/with-deps")
async def with_deps(user: dict = Depends(get_user), db: dict = Depends(get_db)):
    return {"user": user["name"], "db": db["connected"]}


# ── Pydantic body validation ──
@app.post("/items")
def create_item(item: Item):
    return {"ok": True, "sku": item.sku, "qty": item.qty, "tag_count": len(item.tags)}


# ── larger JSON payload ──
@app.get("/list")
def list_items():
    return {"items": [{"id": i, "name": f"item-{i}"} for i in range(20)]}


# ── streaming ──
@app.get("/stream")
async def stream():
    from fastapi.responses import StreamingResponse

    async def gen():
        for i in range(10):
            yield f"chunk-{i}\\n".encode()

    return StreamingResponse(gen(), media_type="text/plain")
'''


@dataclass
class Endpoint:
    name: str
    path: str
    method: str = "GET"
    body: str = ""
    content_type: str = "application/json"


ENDPOINTS: list[Endpoint] = [
    Endpoint("GET /hello (plain JSON)", "/hello"),
    Endpoint("GET /path/42 (path param + int coerce)", "/path/42"),
    Endpoint("GET /headers (header extraction)", "/headers"),
    Endpoint("GET /with-deps (2-level Depends)", "/with-deps"),
    Endpoint("GET /list (20-item list)", "/list"),
    Endpoint(
        "POST /items (Pydantic body validate)",
        "/items",
        method="POST",
        body='{"sku":"SKU-1","qty":3,"tags":["a","b","c"]}',
    ),
]


def _free_port() -> int:
    s = socket.socket()
    s.bind((HOST, 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _wait_http(port: int, path: str = "/hello", timeout: float = 10.0) -> bool:
    import http.client

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection(HOST, port, timeout=0.5)
            conn.request("GET", path)
            conn.getresponse().read()
            conn.close()
            return True
        except Exception:
            time.sleep(0.1)
    return False


def _start_fa(port: int) -> subprocess.Popen:
    """Boot stock FastAPI via uvicorn."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(HERE) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "_bench_app:app",
         "--host", HOST, "--port", str(port),
         "--workers", "1",
         "--log-level", "warning",
         "--no-access-log"],
        cwd=str(HERE),
        env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _start_fr(port: int) -> subprocess.Popen:
    """Boot fastapi-turbo serving the same `app`."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(HERE) + os.pathsep + env.get("PYTHONPATH", "")
    code = f"""
import fastapi_turbo.compat
fastapi_turbo.compat.install()
import sys
sys.path.insert(0, {str(HERE)!r})
from _bench_app import app
app.run({HOST!r}, {port})
"""
    return subprocess.Popen(
        [sys.executable, "-c", code],
        env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _start_gin(port: int) -> subprocess.Popen:
    """Boot the Go Gin reference server (endpoint-identical to the
    Python app)."""
    env = os.environ.copy()
    env["PORT"] = str(port)
    env["GIN_MODE"] = "release"
    return subprocess.Popen(
        [str(GIN_BIN)],
        env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _run_bench(port: int, ep: Endpoint) -> dict | None:
    args = [
        str(BENCH_BIN), HOST, str(port), ep.path,
        str(N_REQS), str(N_WARMUP),
        ep.method, ep.body, ep.content_type,
    ]
    proc = subprocess.run(
        args, capture_output=True, text=True, timeout=120,
    )
    # Sanity: the bench binary exits non-zero if its warmup probe got
    # a 4xx/5xx. Surface that instead of silently returning None +
    # publishing garbage numbers.
    if proc.returncode != 0:
        print(f"  bench-client failed (rc={proc.returncode}): {proc.stderr.strip()}")
        return None
    out = proc.stdout.strip()
    if not out:
        return None
    # Current bench output (src/bin/bench_client.rs):
    #   "  conn=N req=M | p50=Xμs p90=... p95=... p99=... p999=... min=... max=... | N req/s | M MB/s"
    # The percentile stats live between the 1st and 2nd pipe; the req/s
    # value follows the 2nd pipe; optional "server p50=..." line may
    # appear on the next row but we ignore it.
    try:
        first_line = out.splitlines()[0]
        sections = [s.strip() for s in first_line.split("|")]
        if len(sections) < 3:
            raise ValueError(f"unexpected section count: {sections!r}")
        stats_section = sections[1]
        rps_section = sections[2]
        p50 = _extract(stats_section, "p50=")
        p99 = _extract(stats_section, "p99=")
        min_ = _extract(stats_section, "min=")
        rps = float(rps_section.split()[0])
        return {"p50": p50, "p99": p99, "min": min_, "rps": rps}
    except Exception as e:
        print(f"  parse error ({e}): {out!r}")
        return None


def _extract(s: str, needle: str) -> float:
    i = s.find(needle)
    if i < 0:
        return -1.0
    tail = s[i + len(needle):]
    digits = tail.split("μ", 1)[0]
    return float(digits)


def main() -> int:
    if not BENCH_BIN.exists():
        print(f"bench binary missing: {BENCH_BIN}")
        print("build with: cargo build --release --bin fastapi-turbo-bench")
        return 1

    APP_FILE.write_text(APP_SRC)

    fa_port = _free_port()
    fr_port = _free_port()
    gin_port = _free_port()

    print("== fastapi-turbo vs FastAPI+uvicorn vs Go Gin ==\n")
    print(f"Requests: {N_REQS:,} per endpoint (warmup: {N_WARMUP:,})")
    print(f"Client  : compiled Rust bench client, HTTP/1.1 keep-alive\n")

    print("Starting uvicorn (FA)...", end=" ", flush=True)
    fa_proc = _start_fa(fa_port)
    if not _wait_http(fa_port):
        print("FAILED"); fa_proc.terminate(); return 2
    print(f"ok :{fa_port}")

    print("Starting fastapi-turbo...", end=" ", flush=True)
    fr_proc = _start_fr(fr_port)
    if not _wait_http(fr_port):
        print("FAILED"); fa_proc.terminate(); fr_proc.terminate(); return 2
    print(f"ok :{fr_port}")

    gin_proc = None
    if GIN_BIN.exists():
        print("Starting Go Gin   ...", end=" ", flush=True)
        gin_proc = _start_gin(gin_port)
        if not _wait_http(gin_port):
            print("FAILED — continuing without Gin")
            try:
                gin_proc.terminate(); gin_proc.wait(timeout=2)
            except Exception:
                gin_proc.kill()
            gin_proc = None
        else:
            print(f"ok :{gin_port}")
    else:
        print(f"Go Gin binary missing ({GIN_BIN}) — skip.")
        print(f"  Build with: (cd benchmarks/go-gin && go build -o bench-gin .)")

    rows = []
    try:
        for ep in ENDPOINTS:
            print(f"\n{ep.name}")
            print("  FastAPI+uvicorn...", end=" ", flush=True)
            fa = _run_bench(fa_port, ep)
            print(f"p50={fa['p50']:.0f}μs rps={fa['rps']:,.0f}" if fa else "FAILED")
            print("  fastapi-turbo      ...", end=" ", flush=True)
            fr = _run_bench(fr_port, ep)
            print(f"p50={fr['p50']:.0f}μs rps={fr['rps']:,.0f}" if fr else "FAILED")
            gin = None
            if gin_proc is not None:
                print("  Go Gin          ...", end=" ", flush=True)
                gin = _run_bench(gin_port, ep)
                print(f"p50={gin['p50']:.0f}μs rps={gin['rps']:,.0f}" if gin else "FAILED")
            if fa and fr:
                rows.append((ep.name, fa, fr, gin))
    finally:
        for p in (fa_proc, fr_proc, gin_proc):
            if p is None:
                continue
            try:
                p.terminate(); p.wait(timeout=3)
            except Exception:
                p.kill()

    # ── summary ──
    print("\n" + "=" * 80)
    has_gin = any(gin for _, _, _, gin in rows)
    if has_gin:
        print(f"{'Endpoint':<42} {'FA p50':>8} {'FR p50':>8} {'Gin p50':>9} "
              f"{'FR/FA':>7} {'FR/Gin':>7}")
    else:
        print(f"{'Endpoint':<42} {'FA p50':>8} {'FR p50':>8} {'FA rps':>10} "
              f"{'FR rps':>10} {'speedup':>8}")
    print("-" * 90)
    for name, fa, fr, gin in rows:
        fr_vs_fa = fa["p50"] / fr["p50"] if fr["p50"] > 0 else 0
        if gin:
            fr_vs_gin = gin["p50"] / fr["p50"] if fr["p50"] > 0 else 0
            print(f"{name[:42]:<42} "
                  f"{fa['p50']:>7.0f}μs {fr['p50']:>7.0f}μs {gin['p50']:>8.0f}μs "
                  f"{fr_vs_fa:>6.1f}x {fr_vs_gin:>6.1f}x")
        else:
            print(f"{name[:42]:<42} "
                  f"{fa['p50']:>7.0f}μs {fr['p50']:>7.0f}μs "
                  f"{fa['rps']:>10,.0f} {fr['rps']:>10,.0f} "
                  f"{fr_vs_fa:>7.1f}x")

    _write_md(rows)
    return 0


def _write_md(rows: list) -> None:
    from datetime import date

    has_gin = any(gin for _, _, _, gin in rows)
    lines: list[str] = []
    if has_gin:
        lines.append(
            f"### {date.today().isoformat()} — fastapi-turbo vs FastAPI+uvicorn vs Go Gin\n"
        )
    else:
        lines.append(f"### {date.today().isoformat()} — fastapi-turbo vs FastAPI+uvicorn\n")
    lines.append(
        f"*{N_REQS:,} requests per endpoint after {N_WARMUP:,} warmup, "
        f"single connection, HTTP/1.1 keep-alive, compiled Rust bench client.*\n"
    )
    if has_gin:
        lines.append(
            "| Endpoint | FA p50 | FR p50 | Gin p50 | FA p99 | FR p99 | Gin p99 | "
            "FA req/s | FR req/s | Gin req/s | FR vs FA | FR vs Gin |"
        )
        lines.append(
            "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|"
        )
        for name, fa, fr, gin in rows:
            fr_vs_fa = fa["p50"] / fr["p50"] if fr["p50"] > 0 else 0
            gin_cell = f"{gin['p50']:.0f} μs" if gin else "—"
            gin_p99 = f"{gin['p99']:.0f} μs" if gin else "—"
            gin_rps = f"{gin['rps']:,.0f}" if gin else "—"
            fr_vs_gin = (
                f"**{gin['p50'] / fr['p50']:.2f}×**" if gin and fr["p50"] > 0 else "—"
            )
            lines.append(
                f"| {name} | {fa['p50']:.0f} μs | **{fr['p50']:.0f} μs** | {gin_cell} | "
                f"{fa['p99']:.0f} μs | **{fr['p99']:.0f} μs** | {gin_p99} | "
                f"{fa['rps']:,.0f} | **{fr['rps']:,.0f}** | {gin_rps} | "
                f"**{fr_vs_fa:.1f}×** | {fr_vs_gin} |"
            )
    else:
        lines.append(
            "| Endpoint | FA p50 | FR p50 | FA p99 | FR p99 | "
            "FA req/s | FR req/s | speedup (p50) |"
        )
        lines.append("|---|--:|--:|--:|--:|--:|--:|--:|")
        for name, fa, fr, _ in rows:
            speedup = fa["p50"] / fr["p50"] if fr["p50"] > 0 else 0
            lines.append(
                f"| {name} | {fa['p50']:.0f} μs | **{fr['p50']:.0f} μs** | "
                f"{fa['p99']:.0f} μs | **{fr['p99']:.0f} μs** | "
                f"{fa['rps']:,.0f} | **{fr['rps']:,.0f}** | **{speedup:.1f}×** |"
            )
    block = "\n".join(lines) + "\n"
    print("\nMarkdown:\n")
    print(block)
    # Audit R52 finding 3: ``benchmarks/latest_bench.md`` is the
    # CURATED multi-matrix benchmark doc. A naked ``run_bench.py``
    # invocation previously overwrote it with a single small table,
    # silently destroying the richer doc. Now writing happens only
    # when the user opts in via ``--write=<path>`` (or the legacy
    # ``--write-latest-bench`` flag for the canonical filename) —
    # otherwise the table is printed and the curated file stays
    # intact.
    write_target: Path | None = None
    for arg in sys.argv[1:]:
        if arg.startswith("--write="):
            write_target = Path(arg.split("=", 1)[1]).expanduser().resolve()
        elif arg == "--write-latest-bench":
            write_target = ROOT / "benchmarks" / "latest_bench.md"
    if write_target is not None:
        write_target.parent.mkdir(parents=True, exist_ok=True)
        write_target.write_text(block)
        print(f"wrote: {write_target}")
    else:
        print(
            "(not writing — pass --write=<path> or --write-latest-bench "
            "to overwrite a file; the curated benchmarks/latest_bench.md "
            "is preserved by default)"
        )


if __name__ == "__main__":
    sys.exit(main())
