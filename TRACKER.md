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

## P1 — The differential test gate 🔄  ← prerequisite for all destructive work

**KEY FINDING: the gate already exists.** `tests/parity/` is exactly the P1 gate —
`conftest.py::DualServers` runs ONE shared stock-FastAPI app (`parity_app.py`,
"uses ONLY stock FastAPI imports") under BOTH real FastAPI+uvicorn AND turbo's
**Rust `app.run()`** on live loopback ports, and `test_parity.py` diffs
status/headers/JSON/text byte-for-byte. So this isn't build-from-scratch; it's
adopt + extend.

- ✅ **Verified green on the P0 branch: `107 passed` (0.5s)** — Rust `app.run()`
  engine is byte-identical to FastAPI+uvicorn across all 107 cases. The Rust path
  IS driven (not the Python dispatcher). User chose corpus = "reuse now, expand later".
- ⬜ Reduce flake/perf: it spawns 2 subprocess servers per session; fine locally,
  needs the loopback-bind guard in CI (already present in conftest).
- ⬜ EXPAND coverage toward audit-flagged edges before P2 deletions: custom
  `APIRoute` deep nested `Depends`, multipart byte-parity, WS, streaming/file-range,
  exception-handler resolution, middleware ordering. (Per "expand later".)
- ⬜ Wire `tests/parity` as a BLOCKING gate (CI already runs it — `ci.yml:65`
  `pytest tests/parity -x -q`; make its green a hard merge requirement for P2+).
- ⬜ Baseline divergence report: the deeper `run_deep_*_parity.py` runners are the
  real stress corpus — run them on the branch to quantify any Rust-vs-FastAPI gaps
  before deleting the Python dispatcher.

---

## P2 — Crate-substitution levers + two doors 🔄  (gated on the green P1 gate)

### ✅ FIXED: middleware-on-422 parity bug (committed 2d4ad35)
Was: on a 422 the Rust fast path returned the response directly, bypassing the
Python `@app.middleware("http")` chain → middleware headers missing on validation
errors (auth/logging/request-id/CORS), a drop-in violation. Root cause: the mw
wrapper advertised `_has_http_middleware=True` but not
`_fastapi_turbo_defers_extraction_errors`, so Rust (router.rs:2898) returned the
422 instead of deferring it into the chain. Fix (Python-only, `_middleware_wrap.py`,
~12 lines): advertise the defers flag + convert the deferred
`__fastapi_turbo_extraction_errors__` sentinel into a FA-shaped 422 JSONResponse
for raw endpoints (compiled endpoints still raise it after running deps, preserving
dep-exception-pre-empts-422 ordering). Verified: parity P140-143 all pass (P142 was
xfail); full suite 1103 passed (1 unrelated pre-existing flake in
test_multi_range_no_full_file_buffer, passes in isolation).

### Crate-substitution levers — status (each: maturin develop → parity → full suite)
- ❌ **L6 docs HTML — SKIPPED (verified unsafe).** The embedded Swagger/ReDoc
  consts in `server.rs:564,596` are a real fallback for when Python rendering is
  unavailable (`applications.py:7210-7232`, `try/except: pass`, str starts `None`).
  Happy-path parity gate can't cover the degraded path → keep the fallback.
  STRATEGY §4 corrected. (Lesson: verify "dead default" claims before cutting.)
- ✅ **L3 DONE (committed e240ff7) — static Content-Type from Python `mimetypes`.**
  The StaticFiles parity tests caught a real bug: `server.rs mime_for` hardcoded
  `.js`→application/javascript but Starlette→text/javascript (py3.12+). The
  comprehensive check below proved NO Rust table (hardcoded or mime_guess/ServeDir)
  can match Starlette. Fix: Python builds the ext→content-type map from `mimetypes`
  (charset=utf-8 iff text/*) and passes it to Rust via a new `run_server`
  `static_content_types` arg; `mime_for` looks it up (fallback text/plain). Matches
  Starlette by construction on every extension + Python version. Parity gate 135
  passed / 0 xfailed; full suite 1090 passed / 0 failed. (NOTE: kept CachedServeDir —
  the −222 LOC ServeDir swap would REINTRODUCE the mime bug since ServeDir uses
  mime_guess; so that LOC cleanup is NOT worth doing. L3 LOC delta ≈ neutral; the
  win is correctness, not lines.)
  Evidence (comprehensive MIME check, user-requested "all MIME types"): Python
  `mimetypes` vs `mime_guess` 2.0.5 across 35 exts → ServeDir diverges on
  `.mjs/.woff/.xml/.md/.wav/.yaml/.yml/.otf/.map`; Starlette uses Python
  `mimetypes` (version- + OS-dependent), so no Rust table can match. Hence
  Option A (Python passes the map). Verified: parity 135/0xfail, suite 1094/0fail.
- 🟡 **L4 collapse 15-variant 422 shaper** (router.rs:3158-3624) — **safety net
  READY** (committed 5e7e887: `TestValidation422`, 10 byte-for-byte 422 cases, all
  green → Rust 422 == upstream). On reading the code, the 15 functions are mostly
  **thin one-line delegators to 2 real `_impl` fns** (`pydantic_error_response_with_loc_ext_impl`,
  `pydantic_error_to_response_impl`) — so the real LOC win is **modest (~closer to
  −120 than −340)** and it's hot-path. Lower priority than first thought; do it as
  one careful focused edit when convenient, with the gate as the guardrail.
- ⬜ L2 (JSON writer), L7 (async-mechanism consolidation) — later.

### Two doors (the bigger P2)

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
