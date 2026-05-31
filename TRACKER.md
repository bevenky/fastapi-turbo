# fastapi-turbo — Execution Tracker

> Companion to `STRATEGY.md`. Source of truth for phase status.
> Legend: ✅ done · 🔄 in progress · ⏳ blocked/queued · ⬜ not started.
> **Rule:** nothing in P2+ that deletes/replaces behavior merges until the P1
> diff test is green.

Last updated: 2026-05-31.

---

## P0 — Safe cleanup (no behavior change) ✅ COMPLETE & GREEN

Result: **Rust 10,015 → 9,270 LOC (−745); Python pkg −~2,050 LOC.**
**Full suite: 1066 passed, 3 skipped, 0 failed** (drift detectors guarded per
their design); the 2 drift detectors **pass live** (2 passed, 86s). Nothing
committed — staged/untracked for review.

Done:
- ✅ Deleted dead Rust `src/db_pool.rs` + `src/http_client.rs`; removed their
  `mod`/`add_class` from `src/lib.rs`; dropped 6 dead Cargo deps (`reqwest`,
  `tokio-postgres`, `bb8`, `bb8-postgres`, `fastwebsockets`, `mime_guess`; kept
  `multer`). `cargo check` + `maturin develop` green.
- ✅ Deleted out-of-scope add-ons (user decision): `http.py`, `db.py` + their
  3 dedicated tests; salvaged real-parity tests in `test_r52`/`test_r53`;
  updated `test_declared_deps.py`; removed `db` extra + psycopg/redis from
  pyproject `[all]`.
- ✅ Refreshed COMPATIBILITY.md counts (1106→1090 happy, 965→929 FORCE).

Deferred (cosmetic, non-blocking — fold into a later cleanup commit):
- ⬜ Delete legacy `python/fastapi_turbo/_ws_pipe_bridge.py` (present, unimported)
- ⬜ `.gitignore` build artifacts (`.coverage`, `comparison/` venv/node_modules/
  Go binaries); archive stale `gaps_*.md`/`spec.md`
- ⬜ Fix stale doc-comment `__init__.py:54` mentioning removed `fastapi_turbo.db`

## P0.5 — Rust reducibility audit ✅
- ✅ Background workflow complete: 9,352 → ~4,300 realistic minimal (~6,200 crate-only)
- ✅ Findings folded into `STRATEGY.md §4` (per-file table + L1–L7 levers)
- ✅ Levers mapped to phases below (P0 dead-code, P2 crate-subst, P3 pivot-dependent)

### Dead-code deletions (P0, zero-risk, `cargo`+pytest proved it) ✅
- ✅ `rust_hello` (lib.rs + `__init__` import + `__all__` + test refs)
- ✅ `coercion_error_response_indexed` (router.rs, was `#[allow(dead_code)]`)
- ✅ `pydantic_error_response` (router.rs, was `#[allow(dead_code)]`)
- ✅ `drain_one_async_chunk_sync` (streaming.rs, dead)
- ↪️ DEFER to P2 (behind the P1 gate — behavior-adjacent, marked "kept for binary
  stability"): handler_bridge.rs `call_sync_handler` + 4 pass-throughs + the
  `EVENT_LOOP`/`_async_worker` consolidation (lever L7); websocket.rs
  `handle_ws_connection` (needs the WS diff test first).

### Crate-substitution levers (→ P2, behind the P1 gate)
- L1 ServeFile (responses.rs file stack, −540) · L2 JSON writer (−270) ·
  L3 CachedServeDir→ServeDir (−210) · L4 422-shaper collapse (−340) ·
  L6 docs HTML (−146) · L7 async-path consolidation (−150)

### Pivot-dependent levers (→ P3/P4)
- L5 genericize build_router + hand 405/OPTIONS/slash to axum/Starlette (−590) ·
  multipart→Starlette fallback

---

## P1 — The differential test gate ⬜  ← prerequisite for all destructive work

- ⬜ Pick/curate a request corpus (the existing `tests/parity/*` apps are a
  starting set; dedupe the `_r2/_3/_4` variants)
- ⬜ Harness: same app, replay corpus through (a) Rust `app.run()` and (b) real
  FastAPI via uvicorn/ASGITransport; assert **byte-identical** status/headers/body
- ⬜ Wire into CI as a blocking gate
- ⬜ Baseline report: quantify current Rust-engine vs Python-engine divergence
  (this is the first real measurement of it)

---

## P2 — Two doors over the existing engine ⬜  (gated on P1)

- ⬜ Make the Rust core callable as a function (`process_request(scope, body)
  -> (status, headers, bytes)`), not only runnable as a server
- ⬜ Replace `_asgi_dispatch_in_process` body with a thin ASGI shim calling the
  core; keep the irreducibly-Python fallback branches (mounted ASGI sub-apps,
  3rd-party ASGI middleware, raw-ASGI routes)
- ⬜ Same for WS (`_asgi_dispatch_ws_in_process` → shim)
- ⬜ Diff test green for both doors → **delete the ~4,200-line duplicate
  dispatcher** (net ~3,500 LOC)
- ⬜ `app.run(workers=N)` via Rust supervisor + `SO_REUSEPORT` (Robyn-proven)

---

## P3 — The accelerator pivot ⬜  (gated on P1, after P2)

The big one: stop cloning FastAPI, depend on it.

- ⬜ Add real `fastapi` as a dependency; pin a supported version range
- ⬜ `accelerate(app)`: walk `app.routes`, read each `route.dependant` /
  `solve_dependencies` structure → compile to Rust `RoutePlan`
- ⬜ Route compiler handles the common case; everything else → fallback to real
  FastAPI's ASGI app for that route
- ⬜ Replace clones with real upstream behind the green diff test, in order:
  `_openapi.py` → real `app.openapi()`; `_introspect.py` → `route.dependant`;
  `routing.py`/`_resolution.py`/`security.py`; responses/requests/datastructures/
  encoders; param_functions/dependencies/exceptions/status/background/concurrency/
  templating/staticfiles/sse
- ⬜ compat shims: re-export **real** symbols instead of faking; keep only a small
  shim for FastAPI's *internals* drift
- ⬜ `testclient.py` → thin wrapper over real `fastapi.testclient`
- ⬜ Target: `applications.py` 12,067 → ~ (route compiler + glue); total Python
  → ~5K

---

## P4 — Rust reduction ⬜  (informed by P0.5)

- ⬜ Collapse the ~50 `register_*` pyfunctions → one generic registration over a
  Rust `RouteSpec` (fed by the P3 route compiler); shrink `lib.rs`
- ⬜ Apply crate-substitution levers from P0.5 (multer/axum::Multipart,
  tower-http layers, axum IntoResponse where it doesn't fight FastAPI semantics)
- ⬜ Consolidate `handler_bridge.rs` dead/duplicate async paths (`EVENT_LOOP` vs
  `_async_worker`, `#[allow(dead_code)]` variants)
- ⬜ Fix flagged correctness items (CORS regex faked as substring; WS 30s accept
  + per-message timeouts; `HANDLER_CLASS` mutex poison)

---

## P5 — Multi-core + shared-state plane ⬜

- ⬜ Shared-state plane API: `app.state` backing, Rust-shared cache decorator,
  `channel.publish()` cross-worker fan-out, rate-limit dependency on atomics
- ⬜ Pre-fork: `AtomicU64` + KV in `MAP_SHARED`; in-Rust broker over UDS for
  pub/sub; big read-only object held in Rust mem (COW)
- ⬜ "Const requests": startup-cached responses served from Rust (Robyn-borrow)
- ⬜ Free-threaded (3.13t/3.14t): build + CI; audit global `RwLock<Option<Py>>`
  statics + `HANDLER_CLASS` for parallel safety; per-thread event loops
- ⬜ Re-measure no-GIL throughput on PyO3 0.28 (old 3.5× number is stale)

---

## P6 — Decompose & document ⬜

- ⬜ Split residual `applications.py`; refactor any remaining `_try_compile_handler`
- ⬜ Finalize `STRATEGY.md` §4; update `request-flow.html` with final numbers
- ⬜ Update `CLAUDE.md` (architecture now "accelerate real FastAPI, one engine,
  two doors"); reconcile `requires-python` (pyproject >=3.10 vs no-GIL goal)

---

## Open decisions (need owner input)
1. `http.py`/`db.py`: delete outright vs move to `fastapi-turbo-contrib`? *(default: contrib)*
2. FastAPI version pin range for the accelerator (P3).
3. Free-threaded as the *only* endgame, or keep pre-fork as a permanent fallback for un-free-threaded C-exts? *(recommend: keep both; pre-fork is the floor)*

## Benchmarks to run (from AUDIT §8)
- `uvicorn myapp:app` (Python door) vs `app.run()` (native door) throughput
- suspending async-DB endpoint vs sync-DB (prove the single-loop ceiling)
- `to_json` single-pass vs `model_dump` double-pass on a typed endpoint
- pre-fork ×N scaling; free-threaded ×N scaling
