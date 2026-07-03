"""Concurrency wiring assertions — CONCURRENCY.md's measured proofs as a
permanent regression suite.

``benchmarks/matrix/CONCURRENCY.md`` (38ee8c7) established, with per-thread
CPU measurement, WHERE every class of work runs. This module pins the
STRUCTURAL claims — not the microsecond numbers — so a rewiring regression
(e.g. WS frames suddenly routed through the Python worker loop, streams
falling off the inline path, the tokio runtime collapsing to one thread)
fails loudly:

1. ``app.run`` boots a MULTI-THREAD tokio runtime (>= min(4, ncpu) worker
   threads beyond main).
2. WS frame path in thread mode (default) never touches the ``_async_worker``
   loop: its thread's CPU delta stays ~0 across a multi-thousand-frame echo
   burst. Contrast: ``FASTAPI_TURBO_WS_LOOP=1`` lights the loop thread up on
   the identical burst — proving the probe can detect the positive case.
3. Sync streams and cooperative await-streams stay off the worker loop.
   Contrast: ``FASTAPI_TURBO_STREAM_TRAMPOLINE=0`` demotes the same
   await-stream to the worker loop and the probe sees it.
4. GIL share on ``/hello`` is bounded: the Python-attach share of total
   process CPU (measured by subtracting the pure-Rust ``/_ping`` control on
   the identical wire path) stays < 50% — most of a request is GIL-free Rust.
5. ``FASTAPI_TURBO_STREAM_THREAD=1`` produces the documented thread-profile
   signature (dedicated blocking-pool thread appears; the default inline
   one-write path spawns none).

The measurement instrument is a ``/loopstat`` probe endpoint: a SYNC handler
(runs on the tokio request thread) that schedules ``time.thread_time()`` onto
the worker loop via ``call_soon_threadsafe`` and returns it — i.e. the exact
CPU seconds the loop thread has ever burned. Work placed ON the loop is
visible in its delta; work placed anywhere else is not.

CI reality / skip conditions (deterministic-pass on an idle box, SKIP —
never flake — under load):
- whole module skips when loopback binds are denied (``requires_loopback``)
  or ``psutil`` is missing; WS tests skip without ``websockets``.
- every CPU-attribution test pre-checks 1-min load average and skips when
  ``load1/ncpu > _LOAD1_PER_CORE_SKIP``; a failing assertion re-checks load
  and downgrades to a skip when the box got busy mid-test (an idle-box
  failure stays a hard fail). NOTE: load1 decays over ~1 min, so a
  CPU-heavy suite running just before this module on a small-core box can
  legitimately trigger the skip — that is the designed behavior.
- thresholds carry generous margins against the values measured in
  CONCURRENCY.md and re-calibrated at commit time (negative-arm epsilons
  ~1000x above measured, positive-control floors ~2.5-3x below measured —
  per-test comments carry the calibrated numbers); they are CPU-second and
  thread-count structure checks, not latency/rps assertions, so external
  load cannot inflate them (it only slows wall clock, which the generous
  socket timeouts absorb — verified: full pass under a 14-core background
  load burst).
"""

import fastapi_turbo  # noqa: F401 — installs compat shim for `from fastapi ...`

import os
import socket
import subprocess
import sys
import textwrap
import threading
import time

import httpx
import pytest

pytestmark = pytest.mark.requires_loopback

psutil = pytest.importorskip("psutil")

NCPU = os.cpu_count() or 1

# ── Tunables (structure-level, generous margins) ─────────────────────────
# Skip CPU-attribution tests when 1-min load per core exceeds this. Idle dev
# box: ~0.1-0.3. This module's own bursts add < ~0.2/core on an idle box.
_LOAD1_PER_CORE_SKIP = 0.85
# Worker-loop CPU (seconds) the negative arms may burn. Calibrated: measured
# 0.0000 s (loop thread literally cold; probe cost is a few us per call) —
# 50 ms is orders of magnitude of headroom.
_LOOP_EPSILON_S = 0.05
# Worker-loop CPU (seconds) the positive-control arms must burn. Calibrated
# on the CONCURRENCY.md box (18-core, 2026-07-03): WS_LOOP=1 ≈ 0.29 s at
# _WS_FRAMES (3.6 us/frame on the loop), TRAMPOLINE=0 streams ≈ 0.27 s at
# _STREAM_REQS. 0.10 s = ~3x below measured, 2x above epsilon.
_LOOP_POSITIVE_FLOOR_S = 0.10
# WS echo burst size (round-trip frames). Big enough that a loop-routed
# frame path burns >> _LOOP_POSITIVE_FLOOR_S, small enough to finish in ~1 s
# per arm (calibrated: 20k pipelined round-trips took 0.22 s wall).
_WS_FRAMES = 80_000
_WS_CONNS = 2
_WS_BATCH = 100
# Stream burst: requests per arm x (chunks x per-chunk Python work) chosen so
# the generator body costs ~0.6 ms/request — wherever the generator RUNS
# accumulates ~0.25 s CPU, far above _LOOP_POSITIVE_FLOOR_S.
_STREAM_REQS = 400
_STREAM_CHUNKS = 20
# Per-chunk Python-loop iterations inside the stream generator (kept in one
# place: formatted into the app source AND used to compute expected bodies).
_CHUNK_WORK_ITERS = 2500
# /hello GIL-share arm: requests per endpoint. 3000 x ~20 us server CPU
# ≈ 60 ms per delta — far above getrusage noise (< 1 ms).
_HELLO_REQS = 3000
# Minimum credible /_ping CPU delta; below this the measurement is noise
# (e.g. rusage resolution) and the test skips rather than asserts.
_MIN_PING_DELTA_S = 0.010


# ── Loaded-machine guard ─────────────────────────────────────────────────

def _load_reason() -> str | None:
    """Return a skip reason when the machine looks too loaded to attribute
    CPU deterministically, else None."""
    try:
        load1 = os.getloadavg()[0]
    except (OSError, AttributeError):  # pragma: no cover — no loadavg (win)
        return None
    per_core = load1 / NCPU
    if per_core > _LOAD1_PER_CORE_SKIP:
        return (
            f"machine loaded: load1/ncpu = {per_core:.2f} > "
            f"{_LOAD1_PER_CORE_SKIP} (load1={load1:.1f}, ncpu={NCPU})"
        )
    return None


def _skip_if_loaded() -> None:
    reason = _load_reason()
    if reason:
        pytest.skip(f"{reason} — CPU-attribution test needs a quiet box")


def _check(cond: bool, msg: str) -> None:
    """Assert ``cond`` on a quiet box; downgrade to SKIP when the box got
    loaded mid-test (anti-flake), keep a hard FAIL when it stayed idle."""
    if cond:
        return
    reason = _load_reason()
    if reason:
        pytest.skip(f"assertion inconclusive under load ({reason}): {msg}")
    pytest.fail(msg)


# ── Probe app ────────────────────────────────────────────────────────────
# One app source shared by every boot; env kill switches select the wiring
# under test. FASTAPI_TURBO_WORKERS=1 always: single process = unambiguous
# thread/CPU attribution (same discipline as CONCURRENCY.md's w1 runs).

_APP_SRC = """
import fastapi_turbo  # noqa: F401 — installs compat shim

import asyncio
import threading
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

app = FastAPI()

_STREAM_CHUNKS = {stream_chunks}


def _work_chunk(i):
    # ~0.6 ms of Python per {stream_chunks}-chunk stream: big enough that
    # WHERE the generator body runs is unambiguous in thread-CPU deltas.
    acc = 0
    for j in range({chunk_work_iters}):
        acc += j
    return b"%d:%d;" % (i, acc)


@app.get("/hello")
def hello():
    return {{"message": "hello"}}


@app.get("/loopstat")
def loopstat():
    # SYNC handler (never runs on the worker loop itself). Forces the
    # worker loop up on first call, then reads the loop THREAD's cumulative
    # CPU seconds from on the loop.
    from fastapi_turbo import _async_worker as aw

    loop = aw.get_loop()
    box = {{}}
    ev = threading.Event()

    def _read():
        box["cpu"] = time.thread_time()
        ev.set()

    loop.call_soon_threadsafe(_read)
    ok = ev.wait(10)
    return {{"ok": ok, "loop_cpu": box.get("cpu", -1.0)}}


@app.get("/procstat")
def procstat():
    import resource

    ru = resource.getrusage(resource.RUSAGE_SELF)
    return {{"cpu": ru.ru_utime + ru.ru_stime}}


@app.get("/stream-sync")
def stream_sync():
    def gen():
        for i in range(_STREAM_CHUNKS):
            yield _work_chunk(i)

    return StreamingResponse(gen(), media_type="text/plain")


@app.get("/stream-await")
async def stream_await():
    async def gen():
        for i in range(_STREAM_CHUNKS):
            await asyncio.sleep(0)
            yield _work_chunk(i)

    return StreamingResponse(gen(), media_type="text/plain")


@app.get("/stream-sync-small")
def stream_sync_small():
    # Tiny + cheap: always finishes inside the inline drain budget
    # (<< 100 us, << 32 chunks) so the DEFAULT path spawns no thread.
    def gen():
        for i in range(6):
            yield b"chunk-%d;" % i

    return StreamingResponse(gen(), media_type="text/plain")


@app.websocket("/ws")
async def ws_echo(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            msg = await ws.receive_text()
            await ws.send_text(msg)
    except WebSocketDisconnect:
        pass


app.run(host="127.0.0.1", port=__PORT__)
"""


class _Server:
    """A booted single-worker app.run subprocess + measurement helpers."""

    def __init__(self, proc, port, log_path):
        self.proc = proc
        self.port = port
        self.log_path = log_path
        self.base = f"http://127.0.0.1:{port}"
        self.client = httpx.Client(
            base_url=self.base,
            timeout=30.0,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
        )

    # -- measurement probes ------------------------------------------------
    def get(self, path: str) -> httpx.Response:
        r = self.client.get(path)
        assert r.status_code == 200, f"GET {path} -> {r.status_code}"
        return r

    def loop_cpu(self) -> float:
        """Cumulative CPU seconds of the _async_worker loop thread."""
        data = self.get("/loopstat").json()
        assert data["ok"], "loopstat probe timed out — worker loop unresponsive"
        cpu = data["loop_cpu"]
        assert cpu >= 0.0, f"loopstat probe returned {cpu}"
        return cpu

    def proc_cpu(self) -> float:
        """Whole-process CPU seconds (rusage self: all threads)."""
        return float(self.get("/procstat").json()["cpu"])

    def num_threads(self) -> int:
        return psutil.Process(self.proc.pid).num_threads()

    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    # -- lifecycle -----------------------------------------------------------
    def close(self) -> None:
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass


def _boot(tmp_dir, extra_env=None, tag="srv") -> _Server:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    src = textwrap.dedent(
        _APP_SRC.format(
            stream_chunks=_STREAM_CHUNKS, chunk_work_iters=_CHUNK_WORK_ITERS
        )
    ).replace("__PORT__", str(port))
    app_file = tmp_dir / f"wiring_app_{tag}.py"
    app_file.write_text(src)
    log_path = tmp_dir / f"wiring_app_{tag}.log"
    env = dict(os.environ)
    env["FASTAPI_TURBO_WORKERS"] = "1"
    env.update(extra_env or {})
    log = open(log_path, "wb")
    try:
        proc = subprocess.Popen(
            [sys.executable, str(app_file)],
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
        )
    finally:
        log.close()
    deadline = time.monotonic() + 20
    last_err = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(
                f"wiring server ({tag}) died on startup:\n"
                + log_path.read_text(errors="replace")
            )
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/hello", timeout=1.0)
            if r.status_code == 200:
                return _Server(proc, port, log_path)
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(0.05)
    proc.kill()
    pytest.fail(f"wiring server ({tag}) did not become ready: {last_err}")


@pytest.fixture(scope="module")
def default_server(tmp_path_factory):
    """Shared default-wiring server. Tests only take CPU/thread DELTAS, so
    sharing one boot across tests is order-independent."""
    srv = _boot(tmp_path_factory.mktemp("wiring"), tag="default")
    yield srv
    srv.close()


@pytest.fixture()
def boot_server(tmp_path):
    """Factory for per-test servers with kill-switch env vars."""
    servers = []

    def _factory(extra_env=None, tag="srv"):
        srv = _boot(tmp_path, extra_env, tag)
        servers.append(srv)
        return srv

    yield _factory
    for srv in servers:
        srv.close()


# ── WS burst helper ──────────────────────────────────────────────────────

def _ws_burst(server: _Server, total_frames: int) -> int:
    """Echo ``total_frames`` round-trips over _WS_CONNS connections with
    pipelined batches (send _WS_BATCH, then drain _WS_BATCH). Returns the
    number of verified round-trips."""
    from websockets.sync.client import connect

    payload = "x" * 32
    per_conn = total_frames // _WS_CONNS
    done = 0
    for _ in range(_WS_CONNS):
        with connect(
            server.ws_url(), open_timeout=10, close_timeout=5
        ) as ws:
            sent = 0
            while sent < per_conn:
                n = min(_WS_BATCH, per_conn - sent)
                for _ in range(n):
                    ws.send(payload)
                for _ in range(n):
                    got = ws.recv(timeout=10)
                    assert got == payload
                sent += n
            done += sent
    return done


# ── 1. tokio runtime is multi-thread ─────────────────────────────────────

def test_tokio_runtime_is_multithreaded(boot_server):
    """A fresh app.run process must carry a multi-thread tokio runtime:
    >= min(4, ncpu) worker threads beyond main (default runtime = one worker
    per logical core; CONCURRENCY.md verified ncpu+1 threads at fresh boot).
    Also bounds the total: no thread-per-connection / runaway spawning at
    idle."""
    srv = boot_server(tag="tokio")
    # Only /hello (readiness) has run: no worker loop, no blocking threads.
    total = srv.num_threads()
    non_main = total - 1
    min_workers = min(4, NCPU)
    assert non_main >= min_workers, (
        f"expected >= {min_workers} tokio worker threads beyond main, "
        f"found {non_main} (total={total}, ncpu={NCPU}) — multi-thread "
        f"runtime collapsed?"
    )
    # Fresh boot = main + ncpu tokio workers; anything wildly above that
    # means idle thread spawning crept in. Generous slack for allocator /
    # platform helpers.
    assert total <= NCPU + 10, (
        f"idle fresh boot has {total} threads (ncpu={NCPU}) — unexpected "
        f"thread spawning at boot"
    )

    # Darwin bonus: /usr/bin/sample reports thread NAMES, so we can assert
    # the workers really are tokio-rt-workers (calibrated: 18/18 named on
    # the 18-core reference box). Best-effort — sample being unavailable
    # (hardened runtime, non-darwin) must not fail the count assertion
    # above, which is the load-bearing check.
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["/usr/bin/sample", str(srv.proc.pid), "1", "50"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return
        if out.returncode != 0 or "Thread_" not in out.stdout:
            return  # sample denied/empty — count assertion already ran
        named = [
            line
            for line in out.stdout.splitlines()
            if "Thread_" in line and "tokio-rt-worker" in line
        ]
        assert len(named) >= min_workers, (
            f"sample sees only {len(named)} threads named tokio-rt-worker "
            f"(expected >= {min_workers}) — runtime built without the "
            f"multi-thread tokio builder?"
        )


# ── 2. WS frames never touch the worker loop (thread mode) ───────────────

def test_ws_thread_mode_keeps_frames_off_worker_loop(default_server, boot_server):
    """CONCURRENCY.md §3: in thread mode the worker-loop thread accumulated
    0.00 s CPU across ~2.7M frames. Structural form: loop CPU delta ~0
    across a _WS_FRAMES-frame burst, WHILE the identical burst against a
    FASTAPI_TURBO_WS_LOOP=1 server shows the loop burning CPU — the positive
    control that proves the probe detects loop-routed WS work. Both arms in
    one test: a probe that cannot see the positive case must FAIL here, not
    silently pass the negative arm."""
    pytest.importorskip("websockets")
    _skip_if_loaded()

    # Negative arm: default (thread-mode) server.
    cpu0 = default_server.loop_cpu()
    frames = _ws_burst(default_server, _WS_FRAMES)
    assert frames == _WS_FRAMES
    thread_mode_delta = default_server.loop_cpu() - cpu0

    # Positive control: loop-residency mode, identical burst.
    loop_srv = boot_server({"FASTAPI_TURBO_WS_LOOP": "1"}, tag="wsloop")
    cpu0 = loop_srv.loop_cpu()
    frames = _ws_burst(loop_srv, _WS_FRAMES)
    assert frames == _WS_FRAMES
    loop_mode_delta = loop_srv.loop_cpu() - cpu0

    _check(
        loop_mode_delta > _LOOP_POSITIVE_FLOOR_S,
        f"probe validity: WS_LOOP=1 burst of {_WS_FRAMES} frames burned only "
        f"{loop_mode_delta:.3f}s on the worker loop "
        f"(expected > {_LOOP_POSITIVE_FLOOR_S}s) — loopstat probe cannot "
        f"see loop-routed work, so the thread-mode assertion is meaningless",
    )
    _check(
        thread_mode_delta < _LOOP_EPSILON_S,
        f"WS thread mode leaked onto the worker loop: loop CPU delta "
        f"{thread_mode_delta:.3f}s across {_WS_FRAMES} frames (expected "
        f"< {_LOOP_EPSILON_S}s; loop-mode contrast burned "
        f"{loop_mode_delta:.3f}s)",
    )


# ── 3. streams stay off the worker loop ──────────────────────────────────

def _stream_burst(server: _Server, path: str, n: int) -> None:
    expected = b"".join(
        b"%d:%d;" % (i, sum(range(_CHUNK_WORK_ITERS)))
        for i in range(_STREAM_CHUNKS)
    )
    for _ in range(n):
        r = server.get(path)
        assert r.content == expected


def test_streams_stay_off_worker_loop(default_server, boot_server):
    """CONCURRENCY.md §2: default sync streams drain inline and default
    cooperative await-streams ride the request-thread trampoline — the
    worker-loop thread stays cold. Positive control: the SAME await-stream
    under FASTAPI_TURBO_STREAM_TRAMPOLINE=0 demotes to the worker loop
    (Mechanism 2) and the generator's ~0.6 ms/request Python body lights
    the probe up."""
    _skip_if_loaded()

    # Warmup: first await-stream request classifies the gen's code object
    # ON the worker loop (Mechanism 2 proves it cooperative); that cost must
    # not pollute the measured window.
    _stream_burst(default_server, "/stream-await", 5)
    _stream_burst(default_server, "/stream-sync", 5)

    cpu0 = default_server.loop_cpu()
    _stream_burst(default_server, "/stream-sync", _STREAM_REQS)
    sync_delta = default_server.loop_cpu() - cpu0

    cpu0 = default_server.loop_cpu()
    _stream_burst(default_server, "/stream-await", _STREAM_REQS)
    await_delta = default_server.loop_cpu() - cpu0

    # Positive control: trampoline off -> await-streams run ON the loop.
    off_srv = boot_server({"FASTAPI_TURBO_STREAM_TRAMPOLINE": "0"}, tag="tramp0")
    _stream_burst(off_srv, "/stream-await", 5)
    cpu0 = off_srv.loop_cpu()
    _stream_burst(off_srv, "/stream-await", _STREAM_REQS)
    demoted_delta = off_srv.loop_cpu() - cpu0

    _check(
        demoted_delta > _LOOP_POSITIVE_FLOOR_S,
        f"probe validity: TRAMPOLINE=0 burst of {_STREAM_REQS} await-streams "
        f"burned only {demoted_delta:.3f}s on the worker loop (expected "
        f"> {_LOOP_POSITIVE_FLOOR_S}s) — probe cannot see loop-routed "
        f"streams, so the negative arms are meaningless",
    )
    _check(
        sync_delta < _LOOP_EPSILON_S,
        f"sync streams leaked onto the worker loop: delta {sync_delta:.3f}s "
        f"across {_STREAM_REQS} requests (expected < {_LOOP_EPSILON_S}s)",
    )
    _check(
        await_delta < _LOOP_EPSILON_S,
        f"cooperative await-streams leaked onto the worker loop: delta "
        f"{await_delta:.3f}s across {_STREAM_REQS} requests (expected "
        f"< {_LOOP_EPSILON_S}s; TRAMPOLINE=0 contrast burned "
        f"{demoted_delta:.3f}s)",
    )


# ── 4. GIL share on /hello stays bounded ─────────────────────────────────

def test_hello_gil_share_bounded(default_server):
    """CONCURRENCY.md §1: /hello costs ~22.8 us/req of which the GIL-held
    Python share is ~4.2 us — the rest is GIL-free Rust on the identical
    wire path (/_ping control: ~18.6 us). Structural form: total /hello
    CPU < 2x total /_ping CPU for the same request count, i.e. the
    Python-attach share stays < 50% of process CPU. A regression that drags
    protocol work under the GIL (or bloats the attach window) breaks this
    long before it breaks a latency SLO. Measured ratio at commit time:
    ~1.2x; asserting < 2.0x."""
    _skip_if_loaded()

    # /_ping is the built-in pure-Rust baseline route (server.rs) — zero
    # Python per request, same wire path.
    assert default_server.get("/_ping").json() == {"ping": "pong"}

    # Warmup both endpoints (route-state lazy init, connection reuse).
    for _ in range(200):
        default_server.get("/_ping")
        default_server.get("/hello")

    cpu0 = default_server.proc_cpu()
    for _ in range(_HELLO_REQS):
        default_server.get("/_ping")
    cpu1 = default_server.proc_cpu()
    for _ in range(_HELLO_REQS):
        default_server.get("/hello")
    cpu2 = default_server.proc_cpu()

    ping_delta = cpu1 - cpu0
    hello_delta = cpu2 - cpu1

    if ping_delta < _MIN_PING_DELTA_S:
        pytest.skip(
            f"/_ping CPU delta {ping_delta * 1e3:.2f} ms below noise floor "
            f"({_MIN_PING_DELTA_S * 1e3:.0f} ms) — cannot attribute CPU here"
        )
    _check(
        hello_delta > 0,
        f"/hello CPU delta non-positive ({hello_delta:.4f}s) — rusage probe broken?",
    )
    ratio = hello_delta / ping_delta
    _check(
        ratio < 2.0,
        f"GIL share on /hello out of bounds: {_HELLO_REQS} reqs cost "
        f"{hello_delta:.3f}s vs pure-Rust /_ping {ping_delta:.3f}s "
        f"(ratio {ratio:.2f}, expected < 2.0 i.e. Python share < 50% "
        f"of total process CPU)",
    )


# ── 5. STREAM_THREAD=1 kill-switch thread signature ──────────────────────

def test_stream_thread_kill_switch_thread_profile(boot_server):
    """CONCURRENCY.md §2: FASTAPI_TURBO_STREAM_THREAD=1 forces the legacy
    per-stream dedicated blocking thread; the default inline one-write path
    spawns none. Signature check on a cheap always-inline stream: default
    boot shows NO thread growth across a burst; the kill-switch boot grows
    the thread count (blocking-pool thread(s) appear and persist ~10 s).
    Both arms return byte-identical bodies — path change, not behavior
    change."""
    expected = b"".join(b"chunk-%d;" % i for i in range(6))

    dflt = boot_server(tag="inline")
    t0 = dflt.num_threads()
    for _ in range(30):
        assert dflt.get("/stream-sync-small").content == expected
    dflt_growth = dflt.num_threads() - t0

    legacy = boot_server({"FASTAPI_TURBO_STREAM_THREAD": "1"}, tag="legacy")
    t0 = legacy.num_threads()
    for _ in range(30):
        assert legacy.get("/stream-sync-small").content == expected
    legacy_growth = legacy.num_threads() - t0

    _check(
        legacy_growth >= 1,
        f"STREAM_THREAD=1 spawned no blocking-pool thread across 30 streams "
        f"(growth {legacy_growth}) — kill switch not taking the documented "
        f"dedicated-thread path?",
    )
    _check(
        dflt_growth <= 0,
        f"default inline stream path spawned {dflt_growth} thread(s) across "
        f"30 small streams — inline one-write drain regressed to a "
        f"thread-per-stream driver?",
    )
