# Cross-framework benchmark matrix

Fair, audited benchmarks of **fastapi-turbo** (Rust door, `app.run()`) vs
**FastAPI/uvicorn**, **Gin (Go)**, **Fastify (Node)**, and **raw Axum (Rust)**.
All five servers implement the same endpoint CONTRACT (see `app.py` docstring):
byte-comparable responses, identical DB/Redis targets.

## Files

| file | role |
|---|---|
| `app.py` | canonical contract app (turbo + uvicorn run the SAME module) |
| `app_db.py` | deep DB/Redis contract app (driver matrix + writes x commit) |
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
  bottleneck.
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
