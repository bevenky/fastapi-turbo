# fastapi-turbo — TODOs

Open work only. Shipped items are deleted after completion.

---

## FastAPI parity — remaining gaps

All former P0/P1 items (file uploads, FileResponse+Range, StaticFiles,
ASGI middleware bridge, BaseHTTPMiddleware, WebSocket `accept(headers=…)`,
TestClient `websocket_connect`, `redirect_slashes`, 405 handling) shipped
and are covered by the own-repo test suite (currently ~950 tests; see
COMPATIBILITY.md for the live breakdown). The items below are the
long-tail rough edges not yet chased.

### P2 — less common

1. ~~`AsyncClient` shorthand — works today via `httpx.AsyncClient(transport=ASGITransport(app=app))`; convenience re-export in `fastapi_turbo.testclient` would mirror FastAPI's `AsyncClient`.~~ **Shipped 2026-04-23.** `from fastapi.testclient import AsyncClient, ASGITransport` now works directly.
2. ~~`HEAD` auto-handling from `GET` routes (currently explicit 405 — matches FastAPI default, but Starlette can be configured the other way).~~ **Shipped 2026-04-23.** Upstream FastAPI *also* returns 405 on HEAD to a GET-only route. Added regression tests confirming byte-for-byte parity.
3. ~~`OPTIONS` auto-generation for CORS preflight — today the user must register OPTIONS or rely on CORSMiddleware.~~ **Shipped 2026-04-23.** Matches upstream: true preflights go through CORS; non-preflight OPTIONS returns 405 with `Allow: <actually-declared-methods>` (previously returned a hardcoded catch-all list — bug).
4. ~~Multi-range responses (`Range: bytes=0-0,-1` → `multipart/byteranges` 206). Single-range 206 done.~~ **Shipped 2026-04-23.** `FileResponse` emits `206 multipart/byteranges` with per-part `Content-Type` + `Content-Range` headers. Six regression tests in `tests/stress/test_multi_range.py`.
5. ~~Per-route `servers` / `external_docs` in OpenAPI.~~ **Shipped 2026-04-23.** Already wired through both `openapi_extra={'servers': …, 'externalDocs': …}` (upstream-compatible) and the beyond-parity `servers=` / `external_docs=` decorator kwargs. Four regression tests in `tests/stress/test_per_route_openapi_extras.py`.
6. ~~`webhooks=` app parameter + OpenAPI webhooks section.~~ **Shipped 2026-04-23.** `app.webhooks.post(...)` appears under top-level `webhooks` key in the OpenAPI schema; `FastAPI(webhooks=router)` accepts a pre-built router. Four regression tests in `tests/stress/test_webhooks.py`.
7. ~~Custom `APIRoute` via `route_class` — accepted but not fully honoured end-to-end.~~ **Shipped 2026-04-23.** Fixed body-param classification in the custom-route-class path: a bare `BaseModel` / dataclass / `dict[...]` annotation with no explicit marker now defaults to `Body` (matching FA's `get_body_field` heuristic). Four regression tests in `tests/stress/test_route_class_end_to_end.py`: header injection, body-param Pydantic validation (+422), body pre-read consistency, wrapper-level `HTTPException` interception.
8. ~~`operation_id` uniqueness checks and `generate_unique_id_function` plumbing.~~ **Shipped 2026-04-23.** Duplicate `operation_id` emits `UserWarning`; `generate_unique_id_function` honoured at app / router / route levels. Four regression tests in `tests/stress/test_operation_id_and_unique_fn.py`.
9. ~~`dataclass` / `TypedDict` / `msgspec.Struct` as response models (Pydantic models, dicts, lists, and generic aliases already work).~~ **Shipped 2026-04-23.** Dataclasses + TypedDicts both pass through Pydantic's `TypeAdapter` cleanly: filtering, serialisation and OpenAPI schema all match upstream. `msgspec.Struct` is rejected at decoration time — upstream does the same thing (parity outcome is "both say no"). Five regression tests in `tests/stress/test_dataclass_typeddict_response.py`.
10. ~~`Depends(scope="request")` — accepted, treated as default request scope; scope hint not differentiated.~~ **Shipped 2026-04-23.** Observable teardown ordering under `TestClient` matches upstream byte-for-byte for both `scope="function"` and `scope="request"`. `.scope` attribute preserved for introspection. Four regression tests in `tests/stress/test_depends_scope.py`.

---

## Post-audit follow-ups (deferred from the P0/P1/P2 pass — 2026-04-24)

P0 / P1 items are all shipped. P2 status:

### Maintainability — partially shipped

- **`applications.py` split.** Three extracted modules so far:
  `_sentry_compat.py` (383 LoC), `_middleware_wrap.py` (585 LoC),
  `_route_helpers.py` (712 LoC). `applications.py` is now 5,594 LoC
  (from 7,127 — 21% reduction). **Deferred:** the 1,300-LoC
  `_try_compile_handler` monolith. Its inner `_compiled*` closures
  capture ~30 variables from the enclosing function; cleanly
  splitting it needs a design-doc-backed refactor to a
  `HandlerPlan` class (or similar). Schedule alongside the next
  handler-pipeline feature.
- **`except Exception: pass` — all 57 sites now logged.** Each
  still catches `Exception` (behavioural equivalence) but binds
  the exception and emits a DEBUG-level record via
  `fastapi_turbo.applications` logger. Narrowing each to a
  specific exception type (`AttributeError`/`TypeError`/...) is
  deferred — do it incrementally when touching nearby code.
- **PyO3 `downcast` → `cast` migration.** Shipped. 7 call sites
  in `src/responses.rs` + `src/streaming.rs` renamed; the
  crate-level `#[allow(deprecated)]` is gone; `cargo clippy
  -- -D warnings` exits 0.

### Observability / CI

- ~~**CI pipeline.**~~ **Shipped.** GitHub Actions runs on every PR + push to main (`.github/workflows/ci.yml`) AND on every release tag (`.github/workflows/release.yml`). Both gates run: `cargo fmt --check`, `cargo test`, `cargo clippy -- -D warnings`, `ruff check`, fast pytest subset, stress suite, WebSocket suite, parity (real-loopback FastAPI 0.136.0 diff), upstream FastAPI 0.136.0 suite under shim, Sentry-SDK FastAPI + ASGI integration trees. External repos pinned + force-reset every run so reused runners can't drift.
- **Compatibility matrix freshness.** `COMPATIBILITY.md` is a snapshot. Needs an automated "does this row still match reality?" check — e.g., a pytest parameterised over the matrix rows, or a doctest-style assert per claim.

### Benchmark methodology

- **Server-side CPU measurement.** The rewritten `fastapi-turbo-bench` measures wall-time latency + client-side throughput. It doesn't capture server CPU usage under load, warm vs cold state, or memory high-water marks.
- **wrk / oha comparison.** Our bench is single-process. A quick `wrk -c256 -t8` run would cross-validate our numbers against an industry-standard tool.

### Profiling gap

- ~~**Sentry active-thread-id profiling under the manual `SentryAsgiMiddleware(app)` wrap.**~~ **Resolved through R23–R26.** The `test_active_thread_id` cases now pass under the shim and are part of the green 89/89 Sentry-FastAPI integration count. CI gates the full Sentry FastAPI + ASGI integration trees on every PR and release tag.

---

## `fastapi_turbo.audio` helper modules

Opt-in modules that let users build real-time voice-agent apps in pure Python FastAPI code with Rust-native performance on per-frame hot operations. None change the WebSocket API.

### Priority order (by blast radius)

1. **`.codec`** — G.711 μ-law / A-law encode/decode. **CRITICAL for Python 3.13+.** Python removed `audioop` from stdlib in 3.13; any voice-agent Python stack doing G.711 encode/decode via `audioop` is broken with `ModuleNotFoundError`. Rust lookup-table codec (~100 LOC) unblocks all telephony users.

   ```python
   from fastapi_turbo.audio.codec import Mulaw, Alaw
   pcm = Mulaw.decode(ulaw_bytes)    # ~1 μs (table lookup)
   ulaw = Mulaw.encode(pcm_bytes)
   ```

   Users can also install a `sys.modules["audioop"]` shim at app startup so libraries doing `import audioop` work transparently.

2. **`.wav`** — pre-built WAV header streamer. Replaces per-frame `wave.open(BytesIO(), "wb")` (500-1000 μs) with a pre-computed header + 4-byte length update (~5 μs). **100× speedup.** ~70 LOC.

3. **`.record`** — streaming WAV/Opus recorder. Replaces in-memory `bytearray` buffering of entire calls (~115 MB/hour per stream) with streaming-to-disk. Stereo (user L / bot R) built in. Crates: `hound` for WAV, `opus` for Opus. ~200 LOC.

4. **`.resample`** — bytes↔bytes resampler via `rubato` / `libsamplerate` / `speexdsp`. Avoids the numpy allocation hop in typical Python resamplers. 3× speedup. ~140 LOC.

5. **`.pacer`** — precise interval generator via `tokio::time::interval`. Replaces `asyncio.sleep` (1-10 ms jitter) with <100 μs jitter for audio output pacing. ~40 LOC.

6. **`.mixer`** — SIMD saturating PCM mixer for background audio. Replaces NumPy `np.clip(a + b*vol, -32768, 32767)` (62-100 μs/frame) with SIMD i16 add (~5-10 μs). 10× speedup. ~80 LOC.

**Explicitly out of scope:** Rust ONNX Silero VAD wrapper. ONNX inference is the real cost; a Rust wrapper only shaves Python overhead. Users plug any VAD via existing Python async interfaces.

---

## Won't do — rejected designs

Documented so these don't get reopened.

- **`ws.run_echo()` / `ws.run_forward()` / `ws.on("message", handler)`** — non-standard WebSocket extensions. Break the FastAPI mental model. Users should write the standard `while True: await ws.receive_bytes()` loop; we make it fast instead.

- **Fixed-size message batching (`receive_batch(n=5, timeout_ms=20)`)** — batching 5 × 20 ms frames adds 100 ms latency, destroys conversational AI. If timeout-based, it's redundant with the natural async flow.

- **Full SIP / RTP stack** — that's a separate project. Out of scope for a general web framework.

- **Rust ONNX VAD wrapper** — ONNX inference is the bottleneck, not Python boundary cost.
