# Rust-codebase reuse audit (2026-06-03)

**Directive:** our Rust code should be **minimal orchestration/glue** — lean on
Axum, Tokio, tower/tower-http, tokio-tungstenite (via `axum` `ws`), `multer`,
`tokio-stream` at the Rust layer, and real FastAPI/Starlette at the Python layer,
instead of reimplementing. Produced by a 5-agent parallel audit of all `src/*.rs`
(~10,915 LOC).

## Cross-cutting insight (the biggest lever)

The default ASGI door (door B, `process_request*`, oneshot) and `app.run()` (door A,
socket) share **one** assembled router. Much of the Rust middleware stack exists to
make tower-http output **byte-match Starlette** or to **re-derive Starlette routing
semantics** the Python layer already owns. On door B, requests can ride **real
Starlette/FastAPI middleware**, which makes the entire Rust CORS/GZip/TrustedHost/
HTTPSRedirect/redirect-slashes/non-preflight-OPTIONS stack (~430 LOC) redundant for
the common path. The strategic question is whether door A needs Starlette-byte-exact
middleware at all, or can accept stock tower-http output. Target end-state: Rust =
transport + dispatch + PyO3 bridge; everything else = Axum/tower-http/Starlette.

## Two real BUGS found (fix regardless of LOC)

1. **Big-int → lossy float corruption** — `responses.rs` `write_any_json` (~726)
   does `extract::<i64>()` then falls back to `f64`. A Python int > i64::MAX
   serializes as a lossy float on `app.run()` while the Python engine emits the
   exact integer. Two engines, two answers, no error. Fix via `serde_json`
   (arbitrary_precision) or routing ints through Python.
2. **Custom exception handlers ignored under `app.run()`** — `responses.rs`
   `pyerr_to_response` reimplements Starlette's default handlers and **cannot see
   user `@app.exception_handler(...)`** handlers. A handler that works on uvicorn
   is silently bypassed on `app.run()`. Fix by delegating rendering to the app's
   exception machinery.

## Per-file reducible totals (of ~10,915 LOC)

| File | LOC | Realistically reducible | Notable |
|---|---|---|---|
| router.rs | 4140 | ~655–780 | 422 shaping → FastAPI; 405/Allow → axum; async-branch dedup |
| responses.rs | 1834 | ~500–600 | JSON writer fallback → serde_json; MD5/date stack; pyerr → Starlette |
| server.rs | 2022 | ~325 low-risk / ~790 full | embedded docs HTML; CachedServeDir → ServeDir; MW → Starlette |
| multipart.rs + handler_bridge.rs | 1074 | ~800–900 | PyUploadFile → Starlette UploadFile; parser → multer; dead code |
| websocket.rs + streaming.rs | 1275 | ~220–260 | 4 awaitables → 1; dead sync methods; tungstenite auto-close |
| cluster.rs | 492 | 0 (or ~370 if fd-passing dropped) | sendfd + hyper_util core is irreducible |
| config.rs | 20 | ~20 (ServerConfig unused) | |

**Grand total realistically reducible: ~2,500–3,500 LOC (23–32%).** Low-risk quick
wins alone: ~900–1,000 LOC.

## Prioritized plan

### Tier 0 — quick, low-risk, no parity risk (~900–1,000 LOC)
- **Dead code:** handler_bridge.rs 2nd event-loop machine + dead fns + dummy channel
  (~220); websocket.rs 3 dead sync receive methods + `handle_ws_connection` stub
  (~100); server.rs dead `shutdown_signal`, `_oneshot_selftest`→`#[cfg(test)]`,
  `process_request` dedup via `build_inproc_request` (~44); router.rs dead
  `extract_params_to_pydict` wrapper + verify/remove 599 fallback (~80).
- **Dead dependency:** remove `multer` from Cargo.toml (unused) — OR adopt it (M1).
- **config.rs `ServerConfig`** (registered, unused) — delete (~20).
- **websocket.rs collapse 3 receive awaitables → 1 `kind` enum** (~80, pure refactor).
- **server.rs embedded Swagger/ReDoc/OAuth2 HTML constants** — Python already renders
  & passes them (`fastapi.openapi.docs.*`); make required, delete consts (~175).

### Tier 1 — high-value, medium-risk (gate on parity suite)
- **router.rs 422 error-body shaping → real FastAPI** (`jsonable_encoder` +
  validation handler), collapse 2 walkers + message catalog (~400–500). Fixes drift.
- **responses.rs JSON writer fallback → serde_json**, keep orjson fast path, push
  encoding to `jsonable_encoder`/`_json_default` (~250–290). **Fixes big-int bug.**
- **responses.rs `pyerr_to_response` → Starlette exception machinery** (~70–90).
  **Fixes custom-handler bug.**
- **multipart: PyUploadFile/PySyncFile/Immediate → Starlette `UploadFile`** (~420
  incl. the `__init__.py` monkeypatch that only undoes the Rust awaitables).
- **multipart: hand-rolled parser → `multer` (a dep) or Starlette MultiPartParser**
  (~160); consolidate the 3rd Python `email.parser` parser (~95).
- **server.rs CachedServeDir → tower_http::ServeDir** (~210); MD5/HTTP-date stack →
  Python `FileResponse.set_stat_headers` or crates (~150).

### Tier 2 — strategic, parity-test-heavy (do behind the door-B-rides-Starlette lever)
- **server.rs middleware stack → real Starlette on door B** (TrustedHost,
  HTTPSRedirect, redirect-slashes, non-preflight-OPTIONS/Allow, gzip-CL & cors-OK-body
  patches) — ~430 LOC, but R27/byte-exact history; needs the upstream suite green.
- **router.rs 405/Allow → axum MethodRouter** + startup-computed Allow string (~100).
- **cluster.rs**: keep fd-passing (load-aware; macOS SO_REUSEPORT is load-oblivious)
  unless dropping load-aware WS distribution is acceptable (~370, architectural).

## Explicitly KEEP (irreducible PyO3/transport glue)
- streaming.rs (already minimal: `ReceiverStream` + `Body::from_stream` — the model).
- The oneshot/PyO3 bridge (`process_request*`, `PyResponseStream`, scope rebuild).
- `axum::serve` + graceful shutdown + SO_REUSEPORT socket setup.
- cluster.rs `sendfd` + `hyper_util` core + load-aware scheduler.
- `try_coerce_str_to_py` unconstrained-primitive fast path (constrained/non-primitive
  already use real Pydantic `TypeAdapter` via `scalar_validator`).
- yield-dependency teardown protocol (FastAPI exit-stack semantics via PyO3).
- handler_bridge `try-sync probe` + `HANDLER_CLASS` cache (the ~2µs vs ~50µs win).
- WS close-handshake drain loop: needs an `app.run()`-close regression test before
  trusting tungstenite auto-echo (R29/R30 ConnectionReset history).
