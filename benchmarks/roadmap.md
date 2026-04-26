# fastapi-turbo performance roadmap

Where we are and what's left to do, ranked by honest effort-vs-impact.

## Today (baseline, measured this session, all committed)

| Workload | rps | vs FastAPI |
|---|---:|---:|
| HTTP CRUD /items/1 | ~30 000 | ~10× |
| SQLAlchemy + psycopg2 sync, GET /users/1 | 3 982 | 2.93× |
| SQLAlchemy + psycopg3 sync | 3 422 | 2.56× |
| SQLAlchemy + asyncpg async | 2 831 | 1.66× |
| SQLAlchemy + psycopg3 async | 2 826 | 1.29× |
| Redis-py sync GET | 16 584 | 3.82× |
| redis.asyncio GET | 6 945 | 1.60× |

All numbers on loopback Postgres / Redis on Apple silicon. Production
network adds latency to every wire round-trip, where our per-request
round-trip count advantage over uvicorn compounds.

## What I investigated and rejected (with honest reasons)

### Transparent SQLAlchemy AUTOCOMMIT for GET/HEAD/OPTIONS

Premise: RFC 9110 §9.2.2 says GET is safe / idempotent, so SQLA's
default BEGIN/ROLLBACK wrap is semantic dead weight. Skipping it saves
4 wire round-trips per read (~20 μs loopback / ~8-20 ms production).

Prototype result: loopback went from 2 650 rps → 2 210 rps (regression).

Why: SQLA's `isolation_level="AUTOCOMMIT"` switch itself emits a
BEGIN/ROLLBACK probe when applied to an already-acquired connection.
Net wire cost is unchanged — shifted from "per-query wrap" to
"per-connection isolation switch." To actually get the savings, we'd
need a **separate autocommit engine pool** maintained by fastapi-turbo,
which either forks the customer's pool (breaks pool state) or requires
them to wire two engines (customer code change — invariant violation).

Production-latency hypothesis still plausible but unvalidated. Would
need field data from a customer on remote PG to justify the implementation
complexity. **Shelved.**

### Full pyo3-async-runtimes integration (Plan A)

Premise: eliminate the cross-thread submit from request handler to
`_async_worker` loop — save ~25 μs per request by running the Python
coroutine on the same tokio thread that's serving the request.

Architectural constraint discovered during scoping:

- Tokio runs multi-threaded for Rust-side parallelism (HTTP parse,
  parameter extraction, response serialization all happen GIL-free).
- Python asyncio wants ONE loop per thread; `asyncpg.Pool` / async SQLA
  engines bind to the loop at first use and become unusable on other
  loops.
- Three integration choices:
  1. Pin all Python async work to ONE tokio worker (LocalSet pattern).
     **Loses multi-core parallelism for Python handlers.** Same ceiling
     as uvicorn on a single core.
  2. One asyncio loop per tokio worker. **Connection pools can't be
     shared across workers** — customer's single `create_async_engine`
     call becomes a single-threaded bottleneck anyway.
  3. Keep a dedicated loop thread (status quo), route through
     `pyo3-async-runtimes`. **Same cross-thread cost** we already pay —
     zero throughput gain.

No option is strictly better than the current architecture for the
real-world mix of (async handler + shared connection pool + multi-CPU).
The ~25 μs saving is only achievable under option 1, which trades
away a bigger architectural advantage.

**Shelved until a customer workload presents where single-core Python
throughput is the binding constraint** (none in our current benches).

## What IS tractable (ranked by effort-vs-impact, all transparent)

### Phase B — transparent Redis fast-path dispatch (2 weeks)

At fastapi-turbo import time, monkey-patch `redis.asyncio.client.Redis.
execute_command` for the ~25 hottest commands (GET/SET/DEL/HSET/HGET/
MGET/INCR/EXPIRE/…). Route those through a Rust-backed connection
manager using the `redis` crate + tokio. Exotic commands (pipelines,
pub/sub, Lua, cluster) fall through to redis-py untouched.

Projected: `redis.asyncio` 6 945 → ~18–22 K rps (2.5–3×). Customer's
`import redis.asyncio as aioredis; r = aioredis.Redis(pool=...)` is
untouched syntactically.

Risks: redis-py version drift (mitigated with version probe + fallback);
third-party Redis subclasses (our patch lives on the method, so
subclass inheritance works; overrides of `execute_command` retain their
own behavior).

### Phase C.2 — transparent ORM → Core execution for simple reads (1 week)

For routes with a Pydantic `response_model` and a simple
`session.execute(select(Model).where(Model.id == X))` pattern, bypass
the ORM's identity map / instrumented hydration and go direct Row →
Pydantic. Hook `Session._execute_internal` to inspect the statement
shape and route to Core execution when safe. Complex queries
(joins, eager loads, relationships, subqueries) pass through the
normal ORM path.

Projected: ~+25–35% on ORM single-item reads. Transparent.

Risks: statement-shape detection has edge cases; start with a tight
whitelist (single-table select, single scalar where, no joins) and
expand conservatively.

### Phase C.3 — Rust-side response serialization plan (2 weeks)

At route registration, inspect `response_model` and build a field-
emission plan held in Rust. At request time, skip
`model_validate` + `model_dump` — walk the handler's return value by
attribute, emit JSON bytes directly from Rust. Falls back to the
Pydantic path for fields with custom validators or serializers we don't
recognize.

Projected: ~+15–20% on endpoints with Pydantic response models.
Stacks with Phase C.2.

### Phase C.4 — Rust-backed asyncpg / psycopg2 / psycopg3 drop-in via `sys.modules` (4 weeks)

Implement asyncpg's public Python API surface (~20 classes, ~40 Postgres
type codecs) backed by `tokio-postgres`. At `fastapi_turbo` import time
(before SQLA does), `sys.modules['asyncpg'] = fastapi_turbo._asyncpg_rust`.
SQLA's existing asyncpg dialect imports `asyncpg` and transparently
gets our Rust version.

Same pattern for psycopg2 and psycopg3. All three are thin Python
wrappers around C driver libraries; replacing the wrapper saves ~15–25
μs per query (Cython or C call overhead).

Projected: +30–40% on both sync and async Postgres paths (saves the
per-query overhead, not the PG query itself).

Risks: enormous test matrix — every SQL type, every error class, every
async edge case. Needs dedicated weeks and a staged rollout
(environment-flagged first).

## Realistic projection with all phases stacked

| Workload | Today | +B | +C.2 | +C.3 | +C.4 | Go Gin baseline |
|---|---:|---:|---:|---:|---:|---:|
| SQLA async GET | 2 831 | 2 900 | ~3 700 | ~4 200 | **~5 500** | ~14 000 |
| SQLA sync GET (pg2) | 3 982 | 3 982 | ~5 200 | ~6 000 | **~8 000** | ~14 000 |
| Redis async | 6 945 | **~20 000** | — | — | — | ~22 000 |

After all phases: roughly **half of Go Gin** on raw throughput for
Python+SQLA+Pydantic stacks. The remaining gap is pure Python
interpreter overhead. Closing it further requires removing
Pydantic/SQLA (which defeats the reason customers picked them).

## Sequencing recommendation

1. Phase B (Redis) — self-contained, high ROI, low risk. 2 weeks.
2. Phase C.3 (Rust response serialization) — no SQLA coupling, works
   for every endpoint with `response_model`. 2 weeks.
3. Phase C.2 (ORM → Core) — needs careful shape detection, but highly
   localized. 1 week.
4. Phase C.4 (driver replacements) — biggest payoff AND biggest risk.
   Start with asyncpg only since SQLA's async path is where we're
   furthest behind. 4 weeks + staged rollout.

Each phase ships as a normal patch release. Customer's existing code
works identically; they get faster after `pip install --upgrade`.

## What we will NOT promise

- "Go-level performance for Python+SQLA+Pydantic" — not achievable
  without removing Pydantic or SQLA. The library choices set the floor.
- "Magic loop integration that fixes everything" — pyo3-async-runtimes
  is real, useful for some patterns, but doesn't resolve the pool-
  affinity vs multi-core-parallelism dilemma for our target workloads.
- Single-session delivery of multi-week phases above. Each needs
  dedicated work with proper regression testing across the
  fastapi-turbo own suite (see COMPATIBILITY.md for the canonical
  breakdown — pinned to a specific count there rather than
  duplicated in every doc), the upstream FastAPI suite, and the
  parity-runner matrices.
