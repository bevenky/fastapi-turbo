# CONCURRENCY.md — Multi-CPU / GIL wiring: where every microsecond of work runs

**Status: validated 2026-07-03** at HEAD `579fef1`, Python 3.14.4 (GIL build),
18-logical-core macOS box (the standard co-resident-client bench box — absolute
rps is loopback-capped, ratios and CPU accounting are the signal).

Fresh measurements below were taken with the per-thread CPU method from the
prior audits: `ps -M <pid>` per-thread cputime deltas across a 6 s `oha`
window (script: prior-session `prof_streams.sh`), single worker process
(`FASTAPI_TURBO_WORKERS=1`) so thread attribution is unambiguous. Fleet
(w18/w8/w12) numbers are reused from `results_throughput.json` /
`results_db.json` / `results_ws.json` (same box, rounds 7–8).

The one-sentence verdict: **all protocol work (parse, route, body I/O,
serialize-to-wire, frame I/O, compression) runs GIL-free on an 18-thread tokio
runtime and scales across cores; Python execution (param extraction, handler,
dict→JSON write) holds the GIL and is a ≤1-core-per-process budget; every
endpoint family was verified to put its work where the design says it goes.**

---

## 1. HTTP: tokio multi-thread runtime + GIL phase map

### Runtime shape (verified)

`src/server.rs:462` builds the runtime with
`tokio::runtime::Builder::new_multi_thread()` and **no** `.worker_threads()`
override → tokio's default of one worker per logical core. Verified live:
`/usr/bin/sample` of a freshly booted w1 server shows exactly **18 threads
named `tokio-rt-worker` + 1 main thread** (= `hw.ncpu` 18). No thread-name or
count override exists anywhere in `src/` (grep-verified). The only other
threads that ever appear are: the lazily-spawned `_async_worker` uvloop thread
(1 per process, on first suspending coroutine), and tokio `spawn_blocking`
threads (legacy streaming / WS thread-mode handlers).

### Request phase map (code-verified, `src/router.rs::handle_request`, :2533)

| Phase | Where | GIL |
|---|---|---|
| TCP accept, HTTP/1+2 parse | hyper on tokio workers | free |
| Route match (matchit radix), method table | axum/tokio | free |
| Query parse, content-type sniff, header clone decision | `router.rs:2562` "Pure Rust work — no GIL needed" | free |
| Body read | `axum::body::to_bytes(...).await` (`router.rs:2708`) — before any attach | free |
| Tower layers (CORS, gzip, body-limit, redirects) | tokio | free |
| Param extraction + handler call + response conversion (`write_dict_json`) | **one** `Python::attach` (sync no-deps: `router.rs:2966/3007`; async/deps: `block_in_place`→attach `router.rs:3097/3304`) | **held** |
| Write to wire, keep-alive | hyper on tokio workers | free |

Pre-computed route flags (`has_body_params`, `needs_headers`, …) keep the
GIL-free prefix maximal: `/hello` never clones headers, never reads a body.

### Fresh measurement — `/hello`, c64, w1 (2026-07-03)

| endpoint | rps | p50 | CPU/req | total cores | per-thread shape |
|---|---|---|---|---|---|
| `/_ping` (pure Rust, 0 Python) | 161.6k | 0.39 ms | 18.6 µs | **3.01** | flat across tokio workers |
| `/hello` (sync Python handler) | 101.9k | 0.60 ms | 22.8 µs | **2.32** | flat across all 18 workers |
| `/async/hello` | 101.2k | 0.60 ms | 22.7 µs | 2.30 | identical to sync |

Reading it:

- **Total CPU vs GIL-thread CPU:** `/hello` costs 22.8 µs/req total; the pure
  Rust control (`/_ping`) costs 18.6 µs/req on the identical wire path. The
  GIL-held share is therefore **≈4.2 µs/req**, which at 101.9k rps is
  **≈0.43 core-equivalents of GIL time — 43 % GIL utilization** and ~1.9 cores
  of GIL-free Rust running in parallel around it. (Matches the 2026-06-01
  audit's +4 µs Python-crossing figure exactly.)
- **There is no dedicated "GIL thread" for sync handlers** — the per-thread
  deltas are uniform across all 18 tokio workers (0.79–0.81 s each over the
  6 s window): the GIL migrates to whichever worker owns the request, by
  design (`Python::attach` on the request thread, no hop).
- `/async/hello` byte-matches sync: the **try-sync probe**
  (`handler_bridge.rs:243` — first call drives `coro.send(None)`; an
  `AtomicU8` on the route state remembers the verdict) means a coroutine that
  never truly suspends **never touches the worker event loop**. Confirmed by
  CPU shape: no loop thread activity, same 22.7 µs/req.
- Headroom arithmetic: at 43 % GIL utilization the single-process GIL ceiling
  for hello-class handlers is ~235k rps; the observed 102k cap is the
  co-resident client + loopback, not the GIL, and not a server choke
  (`/_ping` shows the same client-side wall at 161k).

Fleet scaling (w18, c100, reused): `/hello` 133.9k rps at 3.6 cores — the GIL
is per-process, so 18 workers ≈ 18 GIL budgets; this box exhausts the
client/loopback first.

---

## 2. Streaming: which driver runs where (post-`dbf9fb8`)

Design (all in `src/streaming.rs::create_streaming_response`):

| Stream class | Driver | Threads touched |
|---|---|---|
| sync generator | inline budget-drain at create (`streaming.rs:117`) — chunks pushed under the GIL already held, Sender dropped **before** `into_response` → hyper emits headers+body+EOF in one vectored write | request (tokio) thread only |
| async gen, proven no-await (bytecode `GET_AWAITABLE` scan) | same inline drain, bare `send(None)` per `__anext__` | request thread only |
| async gen with awaits, **runtime-proven cooperative** (Mechanism 3, `dbf9fb8`) | inline trampoline: eager task on a private non-running request-thread-local loop (`run_stream_trampoline`) | request thread only |
| async gen with **real** awaits | task on the shared `_async_worker` uvloop (Mechanism 2, `schedule_stream_on_worker_loop`) | worker-loop thread (+ tokio for the channel/wire) |
| kill switch `FASTAPI_TURBO_STREAM_THREAD=1` | legacy dedicated `spawn_blocking` thread per stream | blocking pool |

`FASTAPI_TURBO_STREAM_TRAMPOLINE=0` disables Mechanism 3 only (cooperative
streams then stay on the worker loop).

### Fresh verification — c64, w1, 6 s each (2026-07-03)

| run | rps | CPU/req | CPU profile shape |
|---|---|---|---|
| `/stream-sync` default | 73.5k | 23.3 µs | **flat** across tokio workers — inline ✅ |
| `/stream-async` default | 69.9k | 23.5 µs | flat — no-await inline ✅ |
| `/stream-await` default | 55.3k | 26.2 µs | flat, **worker-loop thread cold** — trampoline drives on the request thread ✅ |
| `/stream-await` `TRAMPOLINE=0` | 47.8k | 39.1 µs | **one hot non-tokio thread** (the worker loop, 2.22 s = 36 % of a core; every tokio worker flat at 0.52 s) — Mechanism 2 ✅ |
| `/stream-await` `STREAM_THREAD=1` | 7.2k | 184.5 µs | spread over blocking-pool threads, p50 8.5 ms — legacy per-stream thread+loop ✅ |
| `/stream-sync` `STREAM_THREAD=1` | 11.0k | 324.6 µs | ditto (loses the inline one-write drain) |

Each env switch produces exactly the CPU signature its path predicts; the
default path for **every** stream class in the bench contract runs on the
request thread with zero cross-thread hops (the `dbf9fb8` profile showed the
two hops — enqueue→loop, channel→hyper — were the fleet cap, not CPU).

The await-stream's +2.9 µs/req over no-await is the trampoline machinery
(private-loop bookkeeping + `sleep(0)` future churn), all on-thread. A
mispredicted data-dependent await is finished correctly via
`run_until_complete` and stickily demoted to the worker loop (per real-gen
code object, `_fastapi_turbo_stream_code`).

---

## 3. WebSocket: frame I/O in Rust, Python only for user code

Design (`src/websocket.rs` header, :9–42):

- **Frame I/O** = one Rust `tokio::spawn` select task per connection
  (`websocket.rs:1535`) — read, write, close handshake, ping/pong. Biased
  toward writes. Never touches Python.
- **Python handler** (thread mode, default): dedicated `spawn_blocking` thread
  (`websocket.rs:1447`); receives block on a crossbeam channel with the GIL
  **released** (`RecvAwaitable` never suspends — the whole handler runs inside
  one `coro.send(None)`, no asyncio at all).
- **Batch-drain** (shared `VecDeque` across the 3 recv kinds), **direct-send**
  (outbound frames written inline via one non-blocking poll of the shared
  write half — no select-task wake), and **write-coalescing** (N pipelined
  echoes → one flush/one TCP segment at the batch boundary) are all Rust-side,
  both modes. Kill switches: `FASTAPI_TURBO_WS_DIRECT=0`,
  `FASTAPI_TURBO_WS_COALESCE=0`.
- **Loop mode** (`FASTAPI_TURBO_WS_LOOP=1`, opt-in): handler runs as an
  asyncio task on the shared worker loop instead — no per-connection thread.

### Fresh verification — w1, Rust client (`ws-client-rs`), 2026-07-03

Thread-mode run (20k lat round-trips + 5 s tp at 8 conns × pipeline 16 ≈
2.7 M messages total):

- lat **p50 25.6 µs / p99 38.5 µs**, tp **531k msgs/s** (fast basin).
- Per-thread CPU: frame I/O spread over ~7 tokio workers (select tasks);
  **8 new `spawn_blocking` threads** (~0.98 s CPU each = the Python echo
  handlers, GIL-held); main thread 0.
- **The `_async_worker` loop thread accumulated 0.00 s CPU across the entire
  run** — the WS frame path in thread mode never touches the Python worker
  loop. Claim measured, not inferred.

Loop-mode contrast (`WS_LOOP=1`, same load): exactly **one** new thread — the
worker loop, +3.71 s CPU (all handler execution centralized there), zero
per-connection threads; lat p50 39.1 µs (the per-receive loop wake), tp 610k
(within the bimodal band both modes share).

### The GIL floor

The Python handler **necessarily holds the GIL while executing** — that is
the product: user code is Python. The floor measured with the same Rust
client against all five servers (`results_ws.json`, w8):

| server | true-server echo p50 |
|---|---|
| raw-axum (compiled) | 15.8 µs |
| Fastify | 15.9 µs |
| Gin | 16.4 µs |
| **fastapi-turbo (Python handler)** | **24.9 µs** (w1 fresh run: 25.6) |
| FastAPI/uvicorn | 57.5 µs |

≈9 µs over compiled = one crossbeam wake + one GIL attach + the Python
receive/send frames. Everything Rust could take off the Python thread already
is off it; the residual is the language boundary itself, at 43 % of uvicorn's
cost.

---

## 4. Async: one uvloop per worker process — the 1-core Python ceiling

Architecture (`python/fastapi_turbo/_async_worker.py` +
`src/handler_bridge.rs`):

- ALL suspending async work in a worker process (handlers that really await,
  Mechanism-2 streams, loop-mode WS) runs on **one persistent uvloop on one
  thread** (`_run()` → `run_forever`, eager task factory). Double-checked-lock
  init guarantees exactly one loop no matter how many tokio threads race the
  first submit (P0 regression test: 16 racers → 1 loop).
- Since the loop thread holds the GIL while running Python, **async Python
  throughput is capped at ~1 core per worker process**. This is the identical
  ceiling uvicorn has; it multiplexes I/O fine (sleep(50 ms) scales
  c1→c64 ≈ conc/T) and it scales with **processes**:
  `FASTAPI_TURBO_WORKERS=N` = N loops = N GIL budgets (the fd-passing
  acceptor spreads connections). The DB matrix runs w8 + w12 async for
  exactly this reason.
- Handoff (classic path): tokio thread builds the coroutine →
  `submit_fast` → parks on a Rust `SubmitGate` (GIL **detached**) until the
  loop completes it. Cost ~15.7 µs park/wake; one parked OS thread per
  in-flight async request (inside `block_in_place`, so tokio backfills the
  core).
- **`FASTAPI_TURBO_ASYNC_INLINE=1`** (opt-in): enqueue a Rust job via
  `call_soon_threadsafe`, tokio task awaits a oneshot — zero parked threads;
  extraction + handler run **on the loop thread** (`router.rs:2109` block).

**Pairing rule (measured, commit `afd2fe5`):** `ASYNC_INLINE=1` pays off
exactly when the async client is **loop-resident** — a socket owned by the
worker loop (`contrib_redis` mux client: GET 60→51 µs conn=1, SET +14 % c64,
because the request then runs on the very loop the socket lives on — no
park/wake round-trip). For pool-per-request clients it is a wash (redis-py)
or a small regression (asyncpg c64 −8 %), so the **global default stays
OFF**; flip it per-deployment when using loop-resident clients. Driver policy
stays: turbo → psycopg3-async or asyncpg post-P0; the mux redis client where
throughput matters.

---

## 5. Verdict table — who burns which cores at c64/c100 (fleet data)

turbo rows, `results_throughput.json` (w18, c100) and `results_db.json`
(w8 sync / w12 async, c64), "cores" = whole-process-tree CPU during load:

| endpoint family | rps | cores | where the cores go |
|---|---|---|---|
| hello sync / async | 134k / 134k | 3.6 / 3.7 | tokio Rust HTTP across procs; GIL slices ~0.4 core/proc-equivalent; client/loopback-capped |
| DELETE / XML small | 123k / 122k | 3.7 / 3.9 | same shape as hello |
| POST/PUT/PATCH (body+validate) | 112k | 4.9–5.1 | + body read (Rust, free) + `validate_json` under GIL |
| large JSON | 71k | **14.9** | user dict-build (75 µs) + orjson (32 µs) under GIL — 18 processes each near their 1-core GIL budget; the honest Python-CPU wall |
| stream sync / async | 118k | 4.8–5.0 | request-thread inline drain (§2), one write |
| stream await | 114k | 5.9 | inline trampoline; +hop-free by design |
| redis sync / PG sync | 66–68k / 59k | 5.9–6.6 | handler + driver socket I/O on tokio threads (GIL released during C-socket waits) |
| redis async / PG async | 52–55k / 45–46k | **8.4–9.1** | worker loop (1 GIL core/proc) + submit park/wake tax — more cores for less rps than sync: the handoff, not a hidden serializer |
| WS echo | 152k msgs/s (w8, Rust client) | select tasks (Rust) + 1 blocking thread/conn (GIL) | worker loop provably cold (§3) |

Cross-checks: raw-axum hello = 3.3 cores at 168k on the same harness (client
wall, not server); Gin large-JSON = 43k — turbo's 71k at 14.9 cores is
GIL-parallel-across-processes working as designed.

## Ranked findings: Rust work blocked behind the GIL

Audited for the mandate; **report-only, nothing implemented here.**

1. **GIL-convoy on tokio workers for GIL-bound sync handlers (medium).**
   The sync arms attach directly on the request thread
   (`router.rs:2966/3007`, deliberate — block_in_place removal, round 7). At
   c64 any number of the 18 workers can park in `take_gil` simultaneously;
   a parked worker cannot poll its own local run queue (work-stealing bounds
   but does not eliminate the stall). Evidence: `/hello` c64 p50 +0.21 ms and
   −37 % rps vs `/_ping` while the GIL is only 43 % utilized — queueing/convoy
   loss, not CPU. Candidate fix: a small GIL-entry gate (1–2 permits,
   `tokio::sync::Semaphore`) so surplus workers keep serving Rust phases
   instead of stacking in `take_gil`; or a dedicated attach-pool sized ~2.
   Expected win: c64 tail latency + a slice of the 59.7k rps gap; must A/B
   against conn=1 (any added await costs the fast path).
2. **Classic async submit parks one OS thread per in-flight request (known;
   fix already built).** `block_in_place`→park on SubmitGate
   (`router.rs:3097/3304`) — GIL is detached while parked so this is thread
   pressure, not GIL blocking; `FASTAPI_TURBO_ASYNC_INLINE=1` is the
   implemented remedy, default-flip A/B in flight (tasks #67/#68). Nothing
   new to do from this audit.
3. **WS direct-send writes the socket under the GIL (low).** The direct path
   runs inside the Python `send_text`/`send_bytes` pymethod — the sink poll,
   frame memcpy and write syscall all execute with the GIL held
   (`websocket.rs` direct-send, ~:792–880). At 64 B this is ~1 µs and cheaper
   than a detach/attach pair (~2 µs), so correct as-is for echo-class
   traffic; for multi-KB frames the GIL hold grows linearly. Candidate:
   `py.detach` around the sink write only when `frame_len > ~16 KB`.

**Explicit not-findings** (checked and clean): body read awaits before any
attach; tower gzip/CORS run GIL-free; `/_ping` proves the full wire path needs
zero Python (3.01 cores, flat); streaming inline drains hold the GIL only
while stepping Python generators (irreducible); the WS select task and
batch-drain buffers never attach except to hand a frame to a waiting Python
receiver.

---

*Method note: per-thread attribution = `ps -M` cputime deltas around a 6 s
`oha -c64` window against a `FASTAPI_TURBO_WORKERS=1` server booted from
`benchmarks/matrix/app.py` (`BENCH_ENGINE=turbo`). Thread identity: main is
thread 1, the 18 `tokio-rt-worker`s follow, the `_async_worker` loop (when
spawned) is next, `spawn_blocking` threads appear last. GIL-held CPU/req is
estimated by subtracting the pure-Rust control (`/_ping`) from the target
endpoint on the identical wire path.*
