# Cross-framework benchmark matrix

Fair, audited benchmarks of **fastapi-turbo** (Rust door, `app.run()`) vs
**FastAPI/uvicorn**, **Gin (Go)**, **Fastify (Node)**, and **raw Axum (Rust)**.
All five servers implement the same endpoint CONTRACT (see `app.py` docstring):
byte-comparable responses, identical DB/Redis targets.

The Python apps (`app.py`, `app_db.py`, `app_async_probe.py`) also follow the
**shim import contract**: their ONLY `fastapi_turbo` line is the
`BENCH_ENGINE=turbo` engine-selection import — everything else is plain
`from fastapi import ...` plus third-party drivers (redis-py, asyncpg,
psycopg3/2), and the SAME module runs unmodified under uvicorn. One sanctioned,
fenced exception: the opt-in `fastapi_turbo.contrib_redis` mux-client rows
(`/redis/mux/*` in `app_db.py`), which benchmark that extension itself.
This contract is enforced by `tests/test_shim_completeness.py` (import matrix)
and `tests/test_plain_import_stack.py` (live third-party stack under one
`app.run()` boot).

## Files

| file | role |
|---|---|
| `app.py` | canonical contract app (turbo + uvicorn run the SAME module) |
| `app_db.py` | deep DB/Redis contract app (driver matrix + SQLAlchemy ORM/Core + writes x commit) |
| `run_matrix.py` | conn=1 latency + conn=8 throughput → `results_matrix.json` |
| `bench_throughput.py` | fair all-core throughput + CPU accounting → `results_throughput.json` |
| `run_db_matrix.py` | deep PG/Redis matrix → `results_db.json` |
| `bench_ws.py` | WebSocket echo latency + throughput → `results_ws.json` |
| `verify_parity.py` / `verify_db.py` | correctness gates — run BEFORE benching |
| `gen_report.py` / `gen_db_report.py` | HTML reports (`report.html`, `report_db.html`) |
| `go-gin/`, `fastify/`, `raw-axum/` | the mirror servers |

## Bench hygiene (hard-won rules — violate these and the numbers lie)

1. **Solo-boot.** Exactly ONE server process group alive while measuring.
   Co-resident idle servers inflate conn=1 latency 3-5µs; co-resident *driver
   pools inside one Python process* are far worse (see rule 2).
2. **Isolated boots per backend-driver group (Python engines).** Pools are
   lazy; a boot that only hits one driver's endpoints creates only that
   driver's pool. Measured contamination without this: asyncpg dropped
   8.9k → 3.5k rps (up to 4x) when psycopg3-async's pool shared the
   GIL-bound worker loop. Go/Node/Rust have one native driver — not needed.
3. **Serialize benchmarks.** Never run two oha/bench processes concurrently;
   client and server fight for the same cores and both numbers drop.
4. **Unique ports per framework** (8901 raw-axum, 8902 turbo, 8903 uvicorn,
   8904 gin, 8905 fastify). Runners `kill_port()` + verify the port is free
   before boot; a stale listener silently benches the WRONG server.
5. **Postgres connection budget ≤ 90.** `max_connections=100`; keep
   `workers × pool_max × co-resident-pools` under ~90 or checkouts stall and
   throughput collapses (measured: 23 rps at w18 with the old 4..8 pool).
   Multiprocess Python opens one pool PER worker — budget for it.
6. **Verify parity first.** `python verify_parity.py` / `python verify_db.py`
   before any run — latency on wrong output is meaningless.
7. **Set-based upstream regression check.** When gating changes against
   FastAPI's own test suite, compare the *sets* of FAILED/ERROR test ids
   (sorted, diffed), not the counts — known-flaky tests fluctuate between
   FAILED and ERROR; the sets are the truth.
8. **Warm before measuring.** Runners sleep ~3s post-boot (workers fork,
   pools open) and refresh the psutil procmap before each endpoint so CPU
   accounting captures late-forked workers.

## Driver guidance (measured)

- **Async PG: asyncpg, for BOTH engines.** asyncpg once looked pathological
  under the turbo door; the root cause was an init race in the per-worker
  async loop (`_async_worker.init()`), not the driver. Post-fix (P0):
  asyncpg w8 45.3k rps vs psycopg3-async ~40k, and 26µs loop CPU per op vs
  psycopg3's 45µs. Default in `app.py`/`app_db.py`; override with
  `BENCH_PG_ASYNC_DRIVER` for driver-vs-driver runs.
- **Sync PG: psycopg3 with autocommit reads.** Without autocommit every
  SELECT is BEGIN..COMMIT = 3 wire round trips where pgx/node-postgres do 1.
  asyncpg equivalent: no-op pool `reset` (default reset script is +1 RTT/req).
- **psycopg2 needs TWO fairness shims its successors don't** (both in
  `app_db.pg2_pool`): (1) `getconn()` *raises* `PoolError` when the pool is
  exhausted where psycopg3/asyncpg pools *block* — unguarded at w8/c64 the
  pg2sync rows were 55-64% HTTP-500 error storms (5-15k rps, erratic); a
  `BoundedSemaphore(PMAX)` restores blocking checkout. (2) `putconn()`
  *closes* any connection beyond `minconn` idle, so `minconn=1` means
  perpetual reconnect churn — PMAX=4 measured 2.5k rps of pure connect
  handshakes; `minconn=maxconn` disables the churn. With both fixes psycopg2
  lands within ~10% of psycopg3-sync (raw per-op cost: 34.8 vs 31.2µs read,
  80.1 vs 74.4µs update+commit) — the legacy driver is fine, its *pool* is
  the trap.
- **redis-py: the pooled default client costs ~41µs/cmd; a
  `single_connection_client` measures 25.7µs.** The gap is pool
  checkout/return overhead, not the server. This is an app-level choice and
  applies equally under uvicorn — per-task/worker dedicated connections are
  markedly cheaper than the shared pool for hot paths.
- **SQLAlchemy 2.0: out of the box, and the ORM tax is engine-independent.**
  `/sqla/*` rows (isolated boots `pgm-sqla-sync` w8 / `pgm-sqla-async` w12,
  engines on `isolation_level="AUTOCOMMIT"` for 1-RTT read parity with the
  raw rows, statements built per request). Measured select-one ladder,
  turbo: raw pg3sync 46.3k → Core 36.8k → ORM Session 28.8k req/s
  (≈ +5.6µs/req for Core compile+result, ≈ +7.6µs/req for Session/identity
  map); uvicorn shows the same ladder (20.3k → 17.9k → 15.2k). Async ORM:
  asyncpg 35.0k vs psycopg3-async 29.5k under turbo — asyncpg stays the
  async pick with the ORM too. Smoke-verified: session-per-request via
  `Depends` (generator deps), `app.run()` w1 byte-identical to uvicorn
  across all three engines.

## Worker-count policy (each-at-its-best)

- **CPU-bound endpoints: all cores** (workers=18 on the 18-core box).
  Python throughput scales ~linearly with processes.
- **Python async-heavy groups (pg-async, redis): workers=12.** Each worker
  pins ONE event loop to ONE GIL core; past ~12 workers extra processes only
  add thread thrash — w18 is the WORST async config, w12 beats it by ~25%.
  `BENCH_ASYNC_WORKERS` / `BENCH_DB_ASYNC_WORKERS` override.
- Same fairness philosophy as drivers: every configuration runs its
  measured-best setup, and the reports say so.

## Known floors (not framework overhead — don't chase these)

- **Large JSON**: the payload dict/list is rebuilt in *user code* per request
  (contract requirement). That build dominates; the serializer is not the
  bottleneck. Full arithmetic below.

### Large-JSON conn=1 floor math (why <100µs is not physics-available)

`GET /json/large` = 1000-item dict (28,791 B), rebuilt per request per the
contract. Measured 2026-07-02, release build, SOLO-BOOT, medians of 3
(p50, conn=1, `fastapi-turbo-bench`):

| component                              | µs    | how measured                       |
|----------------------------------------|-------|------------------------------------|
| user dict build (`_large_list()`)      | 75.3  | in-process, median of 9×500 reps   |
| `orjson.dumps` on that dict            | 31.8  | in-process, median of 7 reps       |
| wire + HTTP + 28.8 KB loopback transfer| ~36   | `/_ping` p50 20.5 + ~16 transfer   |
| framework (door)                       | <1.5  | audited previously                 |
| **observed end-to-end p50**            | **144** | sums to ~144.6 — fully accounted |

Reference points same session: `/_ping` 20.5µs, `/hello` 25µs,
`POST /items` 29µs. Cross-framework: Fastify 57µs (V8 builds *and*
stringifies the object in ~24µs total — JIT'd object construction is ~5x
CPython dict building), fair-Gin 114-120µs.

**The bound**: even with a ZERO-cost serializer, 75.3 (build) + ~37
(wire+framework) ≈ **112µs > 100µs**. Sub-100 at conn=1 is impossible
without touching the user-side build, no matter what the framework or
serializer does. What *would* get there:

- **User-side change** (contract-breaking): cache the dict → ~69µs; cache
  pre-encoded bytes → ~40-45µs. Real apps with static-ish payloads should
  do exactly this.
- **A ~2x faster CPython build** (JIT good enough on dict+f-string
  comprehensions, or building the payload in native code). 3.14's
  experimental JIT is nowhere near 2x here; free-threading measured 2-4x
  *slower*.
- Serializer swaps alone cap out at −11µs (below): 144 → ~133µs. Not enough.

### msgspec-instead-of-orjson: measured, rejected (byte parity)

`msgspec.json.encode` (0.21.1) is genuinely faster than `orjson.dumps` on
the contract payloads — large dict 20.8µs vs 31.8µs (0.65x), `/hello` dict
26ns vs 49ns — and byte-identical on every JSON-native contract case
(1000-item dict, unicode incl. astral, i64 edges, None/bool, nested lists,
floats in non-exponent range). Wiring it as the door's preferred dict
serializer is still rejected — it SILENTLY diverges from the
orjson+`_json_default` path (which was aligned byte-for-byte with real
FastAPI) on types it handles natively, *before* the enc_hook can run:

| type                    | orjson path (door today)      | msgspec               |
|-------------------------|-------------------------------|-----------------------|
| float ≥ 1e16            | `1e+16` (= FastAPI/stdlib)    | `1e16`                |
| UTC datetime            | `+00:00` (= FastAPI)          | `Z`                   |
| timedelta               | `90` via `_json_default`      | `"PT90S"` (ISO dur)   |
| bytes/bytearray         | UTF-8 str via `_json_default` | base64                |
| Decimal                 | number via Rust fallback      | `"1.10"` string       |
| str/int subclasses      | native value (`"x"`, `5`)     | enc_hook → `vars()` → `{}` (data loss) |

(Non-UTC tz offsets, date/time/UUID/dataclass/enum/set/NaN/Inf: identical.
0.21.1 has no encoder options to fix any of the divergent rows.)

The "restrict msgspec to clean dicts" fallback is uneconomic: any correct
guard must visit every node with exact-type + float-range checks through
the same C-API traversal that dominates encode cost. msgspec's whole encode
is ~7ns/node; its margin over orjson is only ~3.6ns/node — a pre-scan burns
the margin. Verdict: keep orjson; the serializer is not the reason
/json/large sits at 144µs, and 11µs is not worth silent byte divergence.
- **PG sync throughput (all-core)**: turbo 58.3k req/s = **95%** of
  raw-axum's 61.4k on this box (58,291 / 61,411, `pg item sync`,
  results_throughput.json). That is the compiled-driver ceiling — the +30%
  a from-scratch Rust stack suggests is NOT available on shared-box
  hardware, where the HTTP client, the server, and the Postgres backends
  all compete for the same 18 cores.
- **PG conn=1 latency**: wire RTT ~20µs + asyncpg ~36µs/op + door ~23µs
  ≈ 79µs — fully accounts for turbo's observed 71-73µs `pg item` rows
  (components overlap slightly). raw-axum pays 44-47µs for the same query
  with a compiled driver (tokio-postgres); the delta is driver +
  interpreter cost, not framework overhead.
- **WS RTT (Python handler)**: turbo 24.2µs true-server p50 (Rust client)
  vs Gin 15.3µs is the Python-handler floor: inbound wake (hop1) + 2 GIL
  attaches (receive + send) + interpreter resume ≈ 5-8µs per message. The
  pure-Rust echo path (no Python handler in the loop) measures at axum
  level — it exists for echo-shaped protocols; see benchmarks.md
  "WebSocket Library Comparison (pure Rust echo)".
- **Box confound (all fleet rows)**: client + server + Postgres + Redis
  share one 18-core box. Fleet/throughput rows are comparative — every
  framework carries the same handicap — not absolute capacity for any of
  them.
- **Python CPU work**: 1 core per process, period (GIL). Multi-worker gets
  throughput, never per-request latency. Free-threaded Python measured
  2-4x SLOWER on this workload — not a fix.
- **Postgres write transactions: ~28-34k txn/s on this box, period.** Raw
  floor measured OUTSIDE HTTP (8 processes x 2 conns, psycopg3, tight
  op+COMMIT loop): insert ~27.6k, update ~34.4k, delete ~33.1k txn/s; flat
  from 8 to 64 connections. Two corollaries: (1) every framework's write
  rows (17-25k, incl. raw-Axum at 1.8 cores) sit at 70-80% of that no-HTTP
  ceiling while sharing cores with the PG backends — the uniform
  commit=true band is the DATABASE ceiling, not framework overhead;
  (2) commit=true vs rollback differs ≤10% here because macOS
  `wal_sync_method=open_datasync` does not force a full platter flush —
  this box measures txn machinery, not true fsync durability. Insert rows
  are inherently ~15-20% below update/delete everywhere (new tuple + index
  WAL, table grows during the run).
- **Redis `set_durable`** (SET + WAITAOF, appendfsync=always) measures
  Redis's group-commit fsync floor (~66 rps per in-flight writer on this
  disk). AOF is pre-warmed once per boot (rewrite-complete gate) so rows
  bench in a steady state — compare within a run only.
- **Row validity**: `run_db_matrix.py` records oha's status-code
  distribution per row; >0.5% non-2xx prints `!! row is INVALID` and stores
  `err_pct` in results (added after the pg2sync 500-storm incident).

## Quick start

```bash
source /Users/venky/tech/fastapi_turbo_env/bin/activate
python verify_parity.py && python verify_db.py   # gates
python run_matrix.py                             # conn=1 latency matrix
python bench_throughput.py                       # all-core throughput
python run_db_matrix.py                          # deep PG/Redis matrix
python gen_report.py && python gen_db_report.py  # HTML reports
```

Prereqs: Postgres db `fastapi_turbo_bench` (tables `items`, `bench_writes`),
Redis on 6379 (`bench:item` seeded), `oha` at `/opt/homebrew/bin/oha`,
`go-gin/bench-gin` + `raw-axum/target/release/raw-axum-matrix` built, and
`fastify/node_modules` installed.

## Loop-resident clients × ASYNC_INLINE (measured 2026-07-03, w8 c64)

`FASTAPI_TURBO_ASYNC_INLINE=1` drives async requests on the worker loop itself.
For clients whose socket lives on that loop (`fastapi_turbo.contrib_redis`), the
combo removes the park/wake handoff entirely: mux GET 60→51µs conn=1, 69.0→76.8k
c64; SET 70.5→80.3k (+14%). redis-py rows wash; asyncpg c64 regresses −8%
(loop-side extraction steals driver CPU) — hence the global default stays OFF.
Rule of thumb: loop-resident clients → flag on; asyncpg-heavy apps → flag off.
