# fastapi-turbo — TODOs

Open work only. Shipped items are deleted after completion.

---

## FastAPI parity — remaining gaps

All former P0/P1 items (file uploads, FileResponse+Range, StaticFiles,
ASGI middleware bridge, BaseHTTPMiddleware, WebSocket `accept(headers=…)`,
TestClient `websocket_connect`, `redirect_slashes`, 405 handling) shipped
and are covered by the 510-test suite. The items below are the
long-tail rough edges not yet chased.

### P2 — less common

1. `AsyncClient` shorthand — works today via `httpx.AsyncClient(transport=ASGITransport(app=app))`; convenience re-export in `fastapi_turbo.testclient` would mirror FastAPI's `AsyncClient`.
2. `HEAD` auto-handling from `GET` routes (currently explicit 405 — matches FastAPI default, but Starlette can be configured the other way).
3. `OPTIONS` auto-generation for CORS preflight — today the user must register OPTIONS or rely on CORSMiddleware.
4. Multi-range responses (`Range: bytes=0-0,-1` → `multipart/byteranges` 206). Single-range 206 done.
5. Per-route `servers` / `external_docs` in OpenAPI.
6. `webhooks=` app parameter + OpenAPI webhooks section.
7. Custom `APIRoute` via `route_class` — accepted but not fully honoured end-to-end.
8. `operation_id` uniqueness checks and `generate_unique_id_function` plumbing.
9. `dataclass` / `TypedDict` / `msgspec.Struct` as response models (Pydantic models, dicts, lists, and generic aliases already work).
10. `Depends(scope="request")` — accepted, treated as default request scope; scope hint not differentiated.

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

- **CI pipeline.** No GitHub Actions / CI yet. Targets: `cargo test`, `cargo clippy -- -D warnings`, `pytest tests/`, FastAPI 0.136.0 upstream run, Sentry-SDK integration run, `ruff check`. Each should run on PR open + push to main.
- **Compatibility matrix freshness.** `COMPATIBILITY.md` is a snapshot. Needs an automated "does this row still match reality?" check — e.g., a pytest parameterised over the matrix rows, or a doctest-style assert per claim.

### Benchmark methodology

- **Server-side CPU measurement.** The rewritten `fastapi-turbo-bench` measures wall-time latency + client-side throughput. It doesn't capture server CPU usage under load, warm vs cold state, or memory high-water marks.
- **wrk / oha comparison.** Our bench is single-process. A quick `wrk -c256 -t8` run would cross-validate our numbers against an industry-standard tool.

### Profiling gap

- **Sentry active-thread-id profiling under the manual `SentryAsgiMiddleware(app)` wrap.** Requires thread-ident propagation across tokio→httpx→asyncio. 2 tests fail in `sentry-python/tests/integrations/fastapi/test_fastapi.py::test_active_thread_id`; documented in `COMPATIBILITY.md`.

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
