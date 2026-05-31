# fastapi-turbo — Strategy: from *clone* to *accelerator*

> Status: living document. Started 2026-05-31. Companion: `AUDIT.md` (findings),
> `TRACKER.md` (phase-by-phase execution), `request-flow.html` (diagrams).
> The Rust-reducibility numbers in §4 are refined by a background audit
> (see TRACKER P0).

---

## 0. The thesis in one paragraph

fastapi-turbo today is a **from-scratch reimplementation** of FastAPI + Starlette
in ~33K lines of Python, fronted by a ~10K-line Rust/Axum engine — *and it runs
two request engines in parallel* (a fast Rust path for `app.run()`, a ~4,200-line
pure-Python ASGI dispatcher for everyone else). That dual-clone architecture is
the root cause of both problems: it can't reach Go-level throughput (per-request
Python on the hot path + a single GIL/loop), and it can't *stay* compatible
(54 "Audit R##" rounds chasing upstream). **The strategy is to stop cloning
FastAPI and start *accelerating real FastAPI*:** depend on pip-installed
`fastapi`, read the dependency graph it already computes, compile it to a Rust
execution plan, serve in Rust, and **fall back to real FastAPI for anything Rust
can't execute** — so worst case we are *exactly* as correct as FastAPI, because
we *are* FastAPI. This deletes most of the Python, dissolves the parity
treadmill, and keeps one engine behind two doors.

**Targets:** ~36K → **~13K total** (≈8K Rust asset + ≈5K Python accelerator),
100% FastAPI compatibility *by construction*, Go-class throughput under
`app.run(workers=N)`, and shared in-memory state with **no Redis for a single
node**.

---

## 1. The three-bucket principle (what shrinks, what doesn't)

Every line of code is exactly one of:

| Bucket | Definition | Fate |
|---|---|---|
| **1. Duplicate engine** | Re-implements what the Rust core or real FastAPI already does | **Delete / replace with a thin shim** |
| **2. Non-FastAPI add-on** | Upstream FastAPI never shipped it | **Delete outright** |
| **3. The accelerator + the irreducible** | The Rust engine, the route compiler, the two-door shim, the PyO3 boundary | **Keep — this is the product** |

Compatibility is preserved because **we only ever delete from buckets 1 & 2**.
The public surface users import (bucket 3 + real FastAPI) is never removed.

---

## 2. Target architecture — "Accelerate real FastAPI: one engine, two doors"

```
            user code  ──►  import fastapi   (the REAL pip package)
                              app = FastAPI(); @app.get(...) def h(): ...
                                     │
                fastapi_turbo.accelerate(app)   ← walks app.routes at startup
                                     │           reads route.dependant / solve_dependencies
                                     │           compiles each route → Rust RoutePlan
                                     ▼
                         ┌───────────────────────┐
                         │   ONE Rust engine      │  route match · extract · validate
                         │   (Axum + PyO3)        │  (pydantic-core) · DI · call handler
                         │   + shared-state plane │  · serialize · CORS/compress
                         └───────────┬───────────┘
                  NATIVE DOOR        │        ASGI DOOR
            app.run(workers=N)       │   async __call__(scope,receive,send)
         Rust owns socket+cores+ ────┘     thin shim: receive→core→send
         processing+state plane            (uvicorn / gunicorn / serverless /
         → Go-level throughput              tests / app.mount) → same core,
                                            caller owns network+concurrency
                                                     │
                                     irreducibly-Python fallback branch:
                                     mounted ASGI sub-apps, 3rd-party ASGI
                                     middleware, custom APIRoute, exotic deps
                                     → call REAL FastAPI's ASGI app for that route
```

**Key properties**
- **Two doors, one engine.** `app.run()` and `uvicorn myapp:app` both route
  through the *same* Rust processing core, so they cannot disagree. The diff
  test (P1) proves it.
- **Graceful degradation.** Anything Rust can't execute falls back to real
  FastAPI → correctness is guaranteed, speed degrades smoothly with fast-path
  coverage.
- **The ASGI door is non-negotiable.** It is the drop-in compat surface. We
  *replace its oversized body with a thin shim*, never delete the entrypoint.
- **Shared-state plane (see §5)** is owned by Rust, exposed through the APIs
  users already write.

### Why this guarantees 100% compatibility
`from fastapi import X` returns the **real** `X`. OpenAPI is real FastAPI's
`app.openapi()`. Validation is the user's real pydantic. DI is real
`solve_dependencies`. There is nothing to "stay compatible with" because we
*are* the upstream for everything except the accelerated execution path — and
that path falls back to upstream when unsure. **The R1–R54 parity treadmill
stops permanently.**

---

## 3. What gets deleted vs kept

### Delete (buckets 1 & 2) — target ~20K+ Python LOC + dead Rust
| Code | ~LOC | Bucket | Note |
|---|--:|---|---|
| `_asgi_dispatch_in_process` + WS twin | ~4,200 → shim ~750 | dup | replace w/ two-door shim over Rust core |
| `_openapi.py` | 2,909 | dup | use real `app.openapi()` |
| `_introspect.py` | 2,026 | dup | use real `route.dependant` |
| `routing.py` | 1,313 | dup | use real `APIRouter`/`APIRoute` |
| `_resolution.py` | 736 | dup | use real `solve_dependencies` shape |
| `security.py` | 642 | dup | use real `fastapi.security` |
| compat shims (`fastapi_shim`,`starlette_shim`,…) | ~2,300 | dup | re-export REAL symbols; stop faking |
| `testclient.py` | 2,142 | dup | drive the ASGI door (real `fastapi.testclient`) |
| responses/requests/datastructures/encoders | ~2,200 | dup | use real Starlette/FastAPI |
| param_functions/dependencies/exceptions/status/background/concurrency/templating/staticfiles/sse | ~1,500 | dup | use real FastAPI/Starlette |
| `http.py` | 1,775 | add-on | httpx clone — not FastAPI |
| `db.py` | 238 | add-on | DB pools — not FastAPI |
| `_sentry_compat.py` | 383 | add-on | sentry-sdk's own ASGI integration works once ASGI door is real |
| `_ws_pipe_bridge.py` | 61 | dead | legacy |
| **`src/db_pool.rs`** ✅done | 321 | add-on | dead Rust |
| **`src/http_client.rs`** ✅done | 337 | add-on | dead Rust |
| **dead Cargo deps** ✅done | — | add-on | reqwest, tokio-postgres, bb8, bb8-postgres, fastwebsockets, mime_guess |

### Keep (bucket 3) — the ~5K Python accelerator + ~8K Rust asset
- **Route compiler** — walk real FastAPI routes → Rust plans (~1.5–2K)
- **Two-door entry + ASGI shim + fallback dispatch** (~800)
- **Rust↔FastAPI glue** — app factory, lifespan, handler registration (~800)
- **Response/request boundary adapters** (~500)
- **The Rust engine** — router, responses, server, handler_bridge, multipart,
  websocket, streaming (~8K, see §4 for how far this itself can shrink)

### The honest floor
"100% native FastAPI" has a **fixed cost**: even after the pivot, real FastAPI +
pydantic + Starlette are dependencies the user gets, and our accelerator + Rust
engine is ~13K. **5K total is not reachable without cutting the product**; ~13K
is the honest floor, and it's almost all *asset*, not *liability*.

---

## 4. Rust reducibility (why ~10K, how small honestly)

Current (verified): router.rs 3721, responses.rs 1784, server.rs 1413,
websocket.rs 968, multipart.rs 555, handler_bridge.rs 498, streaming.rs 338,
lib.rs 60, config.rs 20 — **~9,357 after the dead-file deletions** (was 10,015).

The reducibility question — how much of this is hand-rolled work that
axum/tower/tower-http/tokio/multer already provide, vs irreducible PyO3 +
FastAPI-semantics glue — is being quantified by a background audit
(TRACKER P0.5). Hypotheses to confirm:
- **router.rs** is huge partly because of ~50 `register_*` startup pyfunctions;
  a single generic registration over a Rust `RouteSpec` struct (fed by the route
  compiler) could collapse much of that — *and shrinks further under the pivot*
  since FastAPI computes the dependant graph.
- **multipart.rs (555)** may duplicate `multer`/`axum::extract::Multipart`.
- **server.rs** middleware may be thinnable to tower-http layer config.
- **handler_bridge.rs** has dead/duplicate async paths (`EVENT_LOOP` vs
  `_async_worker`, several `#[allow(dead_code)]` variants) to consolidate.
- **Irreducible floor:** the PyO3 boundary, GIL handling, Python coroutine
  driving, and the FastAPI-specific response/validation semantics no crate
  provides. This is the point of the project and cannot be deleted.

### Finalized numbers (P0.5 audit, 2026-05-31)

**Current ~9,352 → realistic minimal ~4,300 LOC** (pivot-on); **~6,200** crate-only.
Split of the current 9,352: **~45% irreducible** PyO3/GIL/coroutine-driving +
FastAPI 422/response *semantics* no crate expresses (~4,200, the floor); **~40%
reducible-via-crate** (~3,700); **~15% deletable/dead** (~1,400, of which ~350 is
strictly `#[allow(dead_code)]`).

| File | Current | Minimal | Dominant lever |
|---|--:|--:|---|
| router.rs | 3721 | ~1,650 | collapse 15-variant 422 shaper→2 fns; genericize build_router; dead code |
| responses.rs | 1784 | ~620 | delete hand-rolled JSON writer; file/range/MD5/date → tower-http `ServeFile` |
| server.rs | 1413 | ~340 | delete 222-LOC `CachedServeDir`→`ServeDir::new`; docs HTML + slash/OPTIONS → Python |
| websocket.rs | 968 | ~830 | mostly irreducible PyO3 awaitables; delete 2 dead fns |
| multipart.rs | 555 | ~340 (→0 under pivot) | hand-rolled parser → multer/axum `Multipart`; else Starlette fallback |
| streaming.rs | 338 | ~290 | mostly irreducible; delete dead `drain_one_async_chunk_sync` |
| handler_bridge.rs | 498 | ~200 | consolidate the **two** parallel async mechanisms into one; ~67 LOC dead |
| lib.rs | 55 | ~12 | delete `rust_hello` |
| config.rs | 20 | 20 | already minimal |

**Reduction levers, ranked** (full detail in TRACKER P2/P3):
- **L1** file/range/ETag/MD5/HTTP-date stack → `tower_http::services::ServeFile` (`fs` feature already on). −540, risk M.
- **L2** delete hand-rolled PyDict→JSON encoder → orjson / `__pydantic_serializer__.to_json` / serde fallback. −270, risk M.
- **L3** delete `CachedServeDir` (222 LOC custom `tower::Service`) → `ServeDir::new`. −210, risk S-M.
- **L4** collapse 15-variant 422 shaper → `error_response()` + `pydantic_to_details()`. −340, risk M.
- **L5** genericize `build_router`; let axum own 405/OPTIONS + Starlette own slash-redirect (3 hand-rolled path-matchers in server.rs go). −590, risk L, **pivot-dependent**.
- **L6** delete embedded Swagger/ReDoc HTML (Python already passes rendered HTML). −146, risk S.
- **L7** consolidate handler_bridge's two async mechanisms (`EVENT_LOOP` path vs `_async_worker` path), delete dead. −150, risk M.

**Correction to an earlier hypothesis:** there are **no `register_*` pyfunctions**
(I'd guessed ~50). Routes already flow as `RouteInfo` structs into one
`build_router`; the collapsible bloat is per-method match arms + 3 external
path-matchers, not 50 functions. The lever is real but smaller.

**The irreducible floor (~4,200) cannot go:** per-param GIL marshalling in
`extract_params_to_pydict_full`, `handle_request`, coroutine driving in
handler_bridge, the WS `#[pyclass]` awaitables, the streaming generator→Body
bridge, `py_to_response` dispatch. You can hand HTTP mechanics to tower-http; you
cannot hand the Python⇄Rust marshalling to any crate.

**Verdict: ~4.3K Rust is credible.** Path: dead-code → P1 gate → crate-substitution → pivot.

---

## 5. The shared-state plane (no Redis, single node)

Rust owns one in-memory shared-state plane; Python workers are stateless compute
over it. **Rule: store Rust-owned bytes/atomics, never live Python objects.**

| State | Free-threaded (Option A) | Pre-fork (Option B/D1) |
|---|---|---|
| Big read-only (ML model) | `Arc<T>`, shared heap | Rust mem before fork → COW (no Py refcount to dirty pages) |
| Counters (rate-limit/metrics) | `AtomicU64` | `AtomicU64` in `MAP_SHARED` — genuinely cross-process |
| KV cache | `DashMap` of bytes | shared-mem map / supervisor over UDS |
| Pub/sub (WS fan-out) | `tokio::broadcast` | in-Rust broker over local IPC (the hard one) |

Exposed drop-in: `app.state.x` backed by the plane, a Rust-shared cache
decorator, `channel.publish()` fanning out across all workers, a rate-limit
dependency on Rust atomics. Caveat: solves **single-node** only; multi-machine
still needs a network store (so does Go).

---

## 6. Multi-core model (V2 recap)

- **Now — pre-fork:** Rust supervisor `fork()`s N workers, each own GIL, shared
  socket via `SO_REUSEPORT` (Robyn-proven). Multi-core today, zero request-path
  risk. Cross-worker state via the shared-memory plane.
- **Endgame — free-threaded (3.13t/3.14t):** one process, N threads, Rust plane
  in shared heap = literally Go's model (`DashMap`≈`sync.Map`,
  `AtomicU64`≈`sync/atomic`, `tokio::broadcast`≈channels), zero IPC, no Redis.
- The request path is **built once** and doesn't change between the two.

---

## 7. Borrowed from Robyn (verified)
- **`SO_REUSEPORT` + cloned listen socket** for shared-nothing multi-process.
- **"Const requests"** — run a handler once at startup, cache the rendered
  response in Rust, serve subsequent hits with zero GIL. Bake it in (improve
  with TTL/invalidation + middleware-safe path).
- **Two knobs:** processes (beat GIL) × workers (Rust threads).
- **Don't copy:** Robyn's actix-actor foundation (we're Axum), its per-process
  `Arc`-only state (the exact gap we fill), no supervisor/respawn.

---

## 8. Sequencing & risk (see TRACKER for tasks)

The pivot is a **bigger, riskier rewrite** than the two-door consolidation. So
sequence safe→risky, and **the diff test gates everything destructive**:

- **P0 — Safe cleanup (no behavior change).** Delete verified-dead code & deps.
  *(Rust dead code done.)*
- **P1 — The gate.** Build the differential test harness (Rust engine vs real
  FastAPI over a request corpus). Nothing destructive merges until this is green.
- **P2 — Two doors over the *existing* engine.** Replace the Python ASGI
  dispatcher body with a thin shim calling the Rust core; keep ASGI fallback.
  Delete the duplicate dispatcher once the diff test passes.
- **P3 — The accelerator pivot.** Depend on real FastAPI; replace the cloned
  Python (openapi/introspect/routing/...) with reads of real FastAPI internals +
  the route compiler. Delete the clones behind the green diff test.
- **P4 — Rust reduction.** Apply the P0.5 audit's crate-substitution levers;
  collapse the `register_*` surface; consolidate handler_bridge.
- **P5 — Multi-core + shared-state plane.** Pre-fork supervisor + shared-memory
  plane; then free-threaded build.
- **P6 — Decompose & document.** Split what remains; finalize docs.

**Two costs to accept openly:** (1) we depend on FastAPI's semi-private
internals (`route.dependant`, `solve_dependencies`) — pin a version range, keep a
small shim for *their* internals; (2) speedup scales with fast-path coverage —
fallback is always correct, just not always fast.
