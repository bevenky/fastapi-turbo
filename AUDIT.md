# fastapi-turbo — Architecture, Performance & Parity Audit

> Date: 2026-05-31. Method: direct code reading + a 67-agent multi-pass audit
> (9 subsystem mappers → 5 dimension auditors → adversarial verifiers → a
> completeness critic; **51 findings → 41 confirmed, 10 refuted**). Every
> load-bearing claim is anchored to `file:line` and was verified in source;
> claims that failed adversarial re-reading were dropped (see end).

---

## 1. Executive summary

1. **The product ships two complete request engines, and which one runs depends
   on how you launch the app.** The Rust Axum core
   (`src/router.rs:1582 handle_request`) serves traffic **only under
   `app.run()`**. The ASGI entrypoint `FastAPI.__call__`
   (`applications.py:7302`) tries a **~3,300-line pure-Python in-process ASGI
   dispatcher first** (`_asgi_dispatch_in_process`, `applications.py:7544-10876`)
   and only falls back to Rust (as a loopback proxy) for requests it declines.
   For all normally-routed requests it serves from Python. **So `uvicorn
   myapp:app` — the single most common FastAPI deployment idiom — runs the
   Python engine and gets roughly stock-FastAPI performance.** This is the
   master finding; it explains both the perf gap and the parity drift.

2. **Most of the test suite exercises the Python engine, not the Rust one most
   users ship.** `TestClient`'s default in-process mode and the upstream
   ~3K-test suite (run via `httpx.ASGITransport`) go through the **Python**
   engine. There *is* a separate real-loopback parity gate that boots the Rust
   server and diffs it against upstream FastAPI — so the Rust path is **not**
   unvalidated (the stronger "suite never touches Rust" claim was refuted in
   verification). But the bulk of day-to-day coverage runs Python, and **there
   is no test that diffs the two engines' responses against each other** — so
   engine *divergence* is the blind spot, and it's why 54 "Audit R##" rounds
   kept patching one copy while the others drifted.

3. **Why not Go-level perf, honestly.** Even on the *fast* Rust path, residual
   per-request Python work remains: `response_model` **double serialization**
   (validate → `model_dump` to dict → Rust re-serializes to JSON), full
   `Request` materialization, BackgroundTasks drained under the GIL. Measured:
   turbo is **6–9× stock FastAPI but only ~60% of its own pure-Rust Axum
   baseline** (31,910 vs 53,085 req/s c=1; POST `/items` 22,945 vs 48,652 =
   **47%**). That gap *to its own Rust core* is the Python-per-request tax.

4. **The ~93K req/s plateau is GIL serialization, not the async loop.** It's
   measured on a *sync* endpoint: sync handlers run under
   `block_in_place`+`with_gil`, so N cores cannot run N Python handlers at once.
   The single shared async loop is a **separate, narrower** ceiling that bites
   the workload the framework is actually sold for — see (5).

5. **For async I/O workloads — the framework's whole selling point — it cannot
   match Go.** Genuinely-suspending async handlers/deps are driven on **one**
   background asyncio thread (`handler_bridge.rs:325-357`). Go gives every
   request a goroutine across all cores; turbo serializes suspending coroutines
   through one loop. This is invisible in the `/hello` benchmark.

6. **Concrete parity break in unchanged FastAPI code:** custom `APIRoute`
   subclasses (a documented feature, commonly used for logging/auth) run a
   hand-rolled pipeline that **does not resolve nested `Depends()` graphs**
   (`_route_helpers.py:701-712` resolves only first-level params). *(High
   confidence; lock it with a 5-line repro.)*

7. **Real, removable bloat ships in every wheel — ~6,900 LOC + the heaviest
   crates:** an httpx reimplementation (`http.py` + `http_client.rs` +
   `reqwest`), Postgres/Redis helpers (`db.py`), and **three fully dead
   artifacts** — `src/db_pool.rs` (unused from Python), and the
   `fastwebsockets` + `mime_guess` Cargo deps (unreferenced). Plus a bespoke
   Sentry shim and a *planned* G.711 telephony codec. None are part of FastAPI.

8. **The drop-in shim itself is sound.** `import fastapi_turbo` installs
   `sys.modules` shims so `from fastapi import ...` works; surface coverage is
   broad. The threat to the drop-in promise is **engine-dependent behavior**
   (§2/§4), not the shim.

9. **Bottom line:** the Rust core is genuinely good (no-GIL routing, one-GIL
   fast path, SIMD multipart, real backpressured streaming). The problem is the
   **Python engine sitting in front of it for ASGI callers**, the **residual
   per-request Python tax on the Rust path**, and **out-of-scope add-ons**.
   Collapse to one engine and shed the add-ons → both goals move at once.
   **And a real head start exists:** the proxy fallback
   (`_asgi_ensure_server` + `_asgi_proxy_http`) *already* boots a Rust server
   and proxies to it, so making `uvicorn` use Rust may be as small as flipping
   `__call__` to prefer the proxy over the Python dispatcher.

---

## 2. Architecture: the request-path problem (the crux)

There are **four full request-lifecycle implementations, plus a fifth partial
one** for custom route classes — selected at runtime.

### Rust path (production via `app.run()`, fast)
`app.run()` → `_fastapi_turbo_core.run_server` (`applications.py:6837/7277`) →
Axum on a multi-threaded tokio runtime (`server.rs:434`, `worker_threads = CPU
cores`). Requests enter `handle_request` (`router.rs:1582`); routing is pure-Rust
no-GIL (matchit radix tree); the fast path is exactly **one GIL acquisition**
(`router.rs:1766/1799`). Well-built. No ASGI/`__call__` references in router.rs.

### Python path (default for ASGI callers, slow)
`FastAPI.__call__` (`applications.py:7302`) is a standard ASGI3 entrypoint:

```
applications.py:7369-7377
  force_proxy = bool(scope.get("_fastapi_turbo_force_proxy"))
  if not force_proxy:
      dispatched = await self._asgi_dispatch_in_process(scope, receive, send)  # ← FIRST
      if dispatched:
          return
  await self._asgi_ensure_server()        # ← only if in-process declined
  await self._asgi_proxy_http(scope, receive, send)
```

`_asgi_dispatch_in_process` (`applications.py:7544-10876`, ~3,332 lines)
reimplements the whole lifecycle **in Python, per request**: host/mount routing
(7589-7705), body drain (7722), param validation (`_coerce` 8497, `_validate`
9138), full dependency resolution (`_resolve_dep` 8575, security scopes 8646),
multipart (10008), streaming/NDJSON, response build. Its own docstring (7339)
says it "Mirrors the Rust router's request handling so the app behaves
identically." A WS twin, `_asgi_dispatch_ws_in_process` (10877-11776), repeats
this for WebSockets. **Returns truthy for all normally-routed requests**, so
ASGI callers are served from Python; only requests it can't handle fall through
to the Rust loopback proxy.

**Who hits the Python path:** `uvicorn`/`gunicorn`, `httpx.ASGITransport`,
serverless ASGI adapters, sub-app mounts, and `TestClient`. Effectively
stock-FastAPI performance with zero Rust acceleration.

### Proxy bridge (third path — and the head start)
`_asgi_ensure_server` (`applications.py:11777`) boots a real loopback Rust
server; `_asgi_proxy_http` (11823) forwards over a socket. **This means a
working Rust bridge for ASGI callers already exists and is tested.**

### Why they diverged → the R## treadmill
Validation + DI + response semantics exist in **four** places — the startup
compiler (`_try_compile_handler`, `applications.py:82-1412`, which emits the
Rust router's plan), the in-process HTTP dispatcher, the in-process WS
dispatcher, and the Rust router — **plus a fifth partial** copy for custom route
classes (`_route_helpers.py:515`). Four-to-five copies of alias handling,
scalar coercion, security-scope propagation, param-model expansion, and 422
shape *guarantee* drift. Each "Audit R##" round patches one copy; the others
lag. **Fixing this is the single highest-leverage change in the repo.**

---

## 3. Performance: why not Go-level

**Measured (repo's own benchmarks):**
- `/hello` GET: **31,910 req/s c=1**, plateau **~93–94K** at c=32–256 (`benchmarks.md:38-49`).
- Pure-Rust Axum baseline, same box: **53,085 req/s c=1** (`benchmarks.md:141`) → **turbo is ~60% of its own Rust core.**
- POST `/items` w/ validation: **22,945 vs 48,652** Rust baseline = **47%** (`latest_bench.md:27`).
- vs stock FastAPI+uvicorn: **6–9×** (`latest_bench.md:179`) — real, but the flattering comparison.
- Go Gin, same box: **~38–40K req/s c=1**. Turbo's c=1 is competitive within ~20%, but **Gin scales across cores under concurrency while turbo plateaus** on the GIL.

**Per-request Python tax on the Rust path (ranked):**
1. **`response_model` double serialization** — validate → `model_dump` → dict → Rust re-serializes. Two passes; dominant cost for typed endpoints.
2. **Full `Request` materialization** (`router.rs:291`) even when the handler uses few fields.
3. **BackgroundTasks** drained holding the GIL (`router.rs:357`).
4. **GIL serialization** — sync handlers under `block_in_place`+`with_gil`; the c=32–256 plateau is GIL contention, not the async loop.
5. **Unconditional Sentry scope work** — `set_request_scope_ctxvar` (5 Rust→Python crossings: `router.rs:1767/1800/1816/1898/2087`) + `_refine_request_scope_for_route` ContextVar copy/set (`_sentry_compat.py:101`) on every request. **No startup guard exists** to skip it when Sentry is absent (the earlier "guarded/cheap" claim was wrong — the guard must be built). Modest but real on a path that should do no unnecessary work.

**The async-I/O ceiling (the real Go gap):** suspending coroutines driven on one
asyncio loop thread (`handler_bridge.rs:46-110`, `325-357`), ~25–50µs/req →
tens of thousands/s regardless of cores. Fixing needs a **loop pool** (one loop
per core thread) or **free-threaded** execution.

**Free-threading:** `CLAUDE.md` recommends GIL-enabled 3.14; `pyproject` says
`>=3.10`. No-GIL (3.13t/3.14t) is the highest-ceiling lever but requires
auditing global `RwLock<Option<Py>>` statics (`router.rs:29-33`) and the
single-loop worker for thread-safety.

*(Refuted by verification — NOT problems: OpenAPI is cached at startup, not
per-request; the Rust router does implement 405; multipart has size caps and
does not panic on crafted bodies; body reads are bounded; CORS regex is not a
silent substring bypass; the PyO3 module does not force-re-enable the GIL on
free-threaded builds. See the full refuted list at the end.)*

---

## 4. Drop-in parity

**The shim works.** `import fastapi_turbo` installs `sys.modules['fastapi']` /
`['starlette']` (`compat/fastapi_shim.py` 1,072, `compat/starlette_shim.py`
473). `from fastapi import ...` resolves to turbo; `import fastapi_turbo as
fastapi` works today.

**The real hazards:**
1. **Engine-dependent behavior (deepest).** Same app under `uvicorn` (Python
   engine) vs `app.run()` (Rust engine) can validate/serialize/error
   differently because the engines are independent reimplementations. A drop-in
   replacement must behave identically regardless of launcher. (§2)
2. **Custom `APIRoute` nested-`Depends` not resolved** (`_route_helpers.py:701-712`) — **verified, high.** (§6)
3. **Three multipart parsers** (Rust `multer`, the in-process Python parser,
   `python-multipart`) must agree byte-for-byte on boundaries, `filename*`
   encoding, size limits.
4. **WebSocket has two non-standard timeouts** — a hard **30s `accept()` cap**
   (`websocket.rs:784`) that 500s a slow auth handshake, *and* a separate
   **per-message receive timeout** (`websocket.rs:873-874`) that `break`s a live
   connection. Starlette has neither by default. *(Low–Medium; verify durations
   vs your handlers.)*
5. **`except Exception: pass` × 57** — DEBUG-logged, but each is a spot the two
   engines can silently diverge.

Security/templating/staticfiles/sessions/sse/background/concurrency are
legitimate FastAPI/Starlette surface — **keep them.**

---

## 5. Bloat / removable (ranked by confidence)

| Item | Weight | Why removable | Risk |
|---|---|---|---|
| `src/db_pool.rs` + `tokio-postgres`/`bb8`/`bb8-postgres` | 321 LOC + crates | **Dead** — not imported by any Python; `db.py` uses psycopg/redis directly | none (verified) |
| `fastwebsockets` dep | crate tree | **Dead** — WS uses axum's own ws | none |
| `mime_guess` dep | crate tree | **Dead** — unreferenced | none |
| `http.py` + `http_client.rs` + `reqwest` (full TLS/h2/socks/brotli/zstd) | 1,775 + ~350 LOC | httpx reimpl; **not FastAPI**; reqwest is the heaviest dep in the tree | low (own import) |
| `db.py` | 238 LOC | Postgres/Redis helpers; **not FastAPI** | low |
| `_sentry_compat.py` | 383 LOC, hot-path hooks | bespoke Sentry; upstream uses sentry-sdk's own ASGI middleware | medium (hot-path coupling) |
| `_asgi_dispatch_in_process` + WS twin | ~4,200 LOC | the second engine; remove after §2 unification | high (do §2 first; biggest single win) |
| planned `fastapi_turbo.audio` (G.711) | `todos.md` | telephony codec, out of scope | none — don't build |
| Doc/test sprawl: `gaps_consolidated.md` 89KB, `spec.md` 47KB, many `tests/parity/*_r{2,3,4}` dupes | ~250KB + dup tests | stale planning + overlapping snapshots | low |
| Committed `.coverage`, `comparison/fastapi-venv/`, prebuilt Go binaries | — | build artifacts in VCS | none (`.gitignore`) |

**Reclaimable immediately (zero parity impact): ~2,700 LOC + 5 heavy crates.**
**After §2 unification: +4,200 LOC → ~6,900 LOC total.**

---

## 6. Correctness & risk

- **Custom `APIRoute` nested-`Depends` gap** (`_route_helpers.py:701-712`) — real bug for a documented feature. **Fix priority 1; add a 5-line repro test.**
- **Engine-divergence heisenbugs** (§2) — the systemic risk; the test suite can't see them (§1.2).
- **WS timeouts** — 30s `accept()` cap (`websocket.rs:784`) + per-message
  receive timeout (`websocket.rs:873-874`); neither exists in Starlette. Low–Medium.
- **No-GIL safety of global statics** `APP_INSTANCE`/`VALIDATION_HANDLER` `RwLock<Option<Py>>` (`router.rs:29-33`) — fine under GIL; re-audit before free-threading.
- **Three multipart parsers / four+ validation paths** — every multi-impl surface is a latent parity bug.
- **`_try_compile_handler`** 1,331-line monolith, inner closures capture ~30 vars (`applications.py:82`) — high complexity, hard to test, central to correctness.
- **Build hygiene:** pyo3 carries both `auto-initialize` (for the embedded bench binary) and `extension-module` (for the `.so`) in one crate — note for the eventual split.

---

## 7. Prioritized roadmap

> Sequencing note (from the critique pass): the engine-collapse is the
> highest-*impact* item but also the riskiest (XL, and it threatens the
> hermetic/no-port test path). It must be **gated behind a diff gate that does
> not exist yet**. So do the safe, high-value wins first; collapse the engine
> only once the gate is green.

### P0 — Safe, high-value, low-risk (do now; no prerequisites)
1. **Build the engine-divergence CI gate first.** Replay one request corpus
   through `app.run()` (Rust) and through the in-process/uvicorn (Python) path
   and assert byte-identical responses. This is the *first* real test that the
   two engines agree, makes the §2 drift visible, and is the hard prerequisite
   for P1. (M)
2. **Switch `response_model` to single-pass serialization** — cache
   `__pydantic_serializer__` at startup (beside the validator, `router.rs:1127`)
   and call `to_json` instead of `model_dump`→dict→`py_to_response`
   (`responses.rs:265-274`). Largest single-request win on the commonest return
   type. *(Magnitude estimated; confirm with a microbenchmark — §8.)* (M)
3. **Delete verified-dead weight:** `src/db_pool.rs` (unused from Python) +
   `tokio-postgres`/`bb8`/`bb8-postgres`; `fastwebsockets` + `mime_guess` deps;
   `rust_hello` (also drop it from `__init__.__all__` + `test_hello.py`). Zero
   parity cost, smaller/faster builds. (S)
4. **Move BackgroundTasks off the GIL/response path** — mirror the correct
   `spawn_blocking` pattern (`responses.rs:455-470`) in the router-injected path
   (`router.rs:333-365`). (S)
5. **Build a Sentry startup guard** (one doesn't exist) so the per-request
   ContextVar work + 5 Rust→Python crossings are skipped when Sentry is absent —
   or remove `_sentry_compat.py` entirely and let users add sentry-sdk's own
   ASGI middleware. (M)

### P1 — Unify on one engine (highest impact; gated behind P0.1)
6. Once the diff gate is green, make production ASGI callers
   (`uvicorn`/`gunicorn`) route to Rust — the **already-built** `_asgi_proxy_http`
   loopback (`applications.py:11823`) is the existing bridge. **Caveat:** the
   in-process Python path exists deliberately for the **hermetic / no-port**
   use case (`httpx.ASGITransport`, sandboxed CI — see the `__call__` docstring
   at `applications.py:7314`). Do *not* make proxy-for-all the blanket default;
   keep an in-process path for sandboxed tests, ideally executing the *same*
   compiled plan as Rust. (XL)
7. After the gate proves parity, delete `_asgi_dispatch_in_process` +
   `_asgi_dispatch_ws_in_process` (~4,200 LOC). `applications.py` drops ~⅓. (L)
8. Decide `http.py`/`http_client.rs`/`reqwest` + `db.py`: move to an optional
   `fastapi-turbo-contrib` package or feature-gate (`Cargo.toml` has **no
   `[features]` table** today, so everyone compiles them). Not FastAPI. (M)
9. `.gitignore` build artifacts; untrack `comparison/` node_modules; collapse
   `gaps_*`/`spec.md` into one short `STATUS.md`. (S)

### P2 — Residual Rust-path tax + the async ceiling
10. Lazy `Request` materialization — build only declared fields; drop the dead
    `query_params` dict (`router.rs:224-228`). (M)
11. **Quantify, then fix, the async-I/O ceiling.** First benchmark a genuinely
    *suspending* async endpoint (the current ~93K plateau is a sync endpoint —
    §8). If confirmed, replace the single shared loop (`handler_bridge.rs:325`)
    with a per-worker-thread loop pool, or pursue free-threaded execution after
    auditing the global `RwLock<Option<Py>>` statics. (XL — design-doc first)

### P3 — Decompose `applications.py`
12. After P1 deletes the dispatchers, refactor the 1,331-line
    `_try_compile_handler` into a `HandlerPlan` class and split into
    routing/openapi/lifespan modules; target <2,000 lines. (L)

---

## 8. Open questions / needs measurement

- **Quantify the `uvicorn` penalty:** benchmark the *same* app under
  `uvicorn myapp:app` (Python engine) vs `app.run()` (Rust). README numbers
  only reflect `app.run()`; the standard deployment is likely ~stock FastAPI.
- **`response_model` double-serialization cost:** profile a typed endpoint to
  size P2.8.
- **Loop-pool feasibility:** prototype N-loop dispatch; measure async-I/O
  scaling vs Go.
- **No-GIL viability** on 3.14t: does the global-statics audit allow it, and
  what's the real throughput multiple?
- **Differential corpus size** needed to catch the known engine divergences
  before deleting the Python engine.

---

### Verification provenance
51 candidate findings → **41 confirmed, 10 refuted** by adversarial re-reading.
Refuted (i.e. NOT real problems — do not chase these): "suite never tests the
Rust path" (a real-loopback parity gate exists); crafted multipart panics the
Rust parser; unbounded-memory body-read DoS; CORS `allow_origin_regex` substring
bypass; `allow_origins=['*']` + credentials panic; PyO3 module re-enables the
GIL on free-threaded builds; hand-rolled JSON emits invalid Decimal NaN/Inf;
range parse underflow on empty files; per-request Sentry path panics on missing
`__module__`. The findings above reflect the confirmed set, with the critique
pass's corrections folded in (engine-test framing, Sentry has no existing guard,
WS has two timeouts, roadmap re-sequenced).
