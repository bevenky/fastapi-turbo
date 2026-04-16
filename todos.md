# fastapi-rs — TODOs

Open work only. Shipped items are deleted after completion.

---

## FastAPI parity — critical gaps

Standard FastAPI code hits these and breaks or silently misbehaves.

### P0 — quick wins (small LOC, high blast radius)

1. **`response_model_by_alias` + Pydantic `Field(alias=...)`** — responses ignore field aliases. Breaks every API using Pydantic aliases. Fix: accept `response_model_by_alias: bool = True` on routes, thread through `_apply_response_model`, call `model_dump(by_alias=...)`. Expose `pydantic.Field` from shim. ~80 LOC.

2. **`default_response_class`** at `FastAPI()`, `APIRouter()`, and route decorator level. Can't make `ORJSONResponse` the app default today. ~30 LOC.

3. **`FastAPI(responses=...)`** — app-level default response schemas (404/500 shapes applied to all routes). ~15 LOC.

4. **`FastAPI(debug=...)`** — debug tracebacks on 500. ~5 LOC.

5. **`ORJSONResponse` actually using orjson** — currently falls back to `json`. ~15 LOC.

### P0 — large

6. **File uploads** — `UploadFile` is a stub, `File(...)` marker not wired, multipart body parsing missing. Blocks the whole file-upload category of apps. ~250 LOC (Rust multipart parser + Python interface).

7. **`FileResponse` + `Range: bytes=...` header support** — no way to return a file with proper content-type, no video/resumable download support. ~180 LOC.

8. **`StaticFiles` mount serving files at scale** — mount helper exists but isn't wired. ~100 LOC.

### P1 — common features

9. **`SessionMiddleware`** — session cookie helper. ~80 LOC.

10. **`AuthenticationMiddleware`** — `request.auth` / `request.user` population. ~50 LOC (paired with `request.auth`/`request.user` properties on Request, ~15 LOC).

11. **ASGI middleware bridge** — Sentry, OpenTelemetry, Prometheus middleware can't be installed today (we only accept Tower middleware). ~150 LOC.

12. **`OAuth2ClientCredentials` + `OpenIdConnect`** security schemes. ~90 LOC.

13. **Pydantic v2 decorators on response_model**: `computed_field`, `field_serializer`, `model_serializer`, `model_validator` — not exercised in our response path. ~120 LOC.

14. **`request.stream()`** — read request body in chunks. ~30 LOC.

15. **Custom response headers on `accept(headers=...)`** — axum's `WebSocketUpgrade` doesn't expose them; needs hyper-level escape hatch. ~60 LOC.

### P2 — less common

16. TestClient WebSocket support (`client.websocket_connect(...)`).
17. `AsyncClient` / `ASGITransport` for async tests.
18. `HEAD` auto-handling from `GET` routes.
19. `OPTIONS` auto-generation for CORS preflight.
20. `405 Method Not Allowed` (currently returns 404 on wrong method).
21. Request size limits enforcement.
22. `redirect_slashes` parameter.
23. `dataclass` / `TypedDict` / `msgspec.Struct` as response models.
24. Per-route `servers` / `external_docs`.
25. `webhooks=` app parameter + OpenAPI webhooks section.
26. Multipart range responses (206 multipart).
27. Custom `APIRoute` via `route_class`.
28. `operation_id` uniqueness checks.

---

## `fastapi_rs.audio` helper modules

Opt-in modules that let users build real-time voice-agent apps in pure Python FastAPI code with Rust-native performance on per-frame hot operations. None change the WebSocket API.

### Priority order (by blast radius)

1. **`.codec`** — G.711 μ-law / A-law encode/decode. **CRITICAL for Python 3.13+.** Python removed `audioop` from stdlib in 3.13; any voice-agent Python stack doing G.711 encode/decode via `audioop` is broken with `ModuleNotFoundError`. Rust lookup-table codec (~100 LOC) unblocks all telephony users.

   ```python
   from fastapi_rs.audio.codec import Mulaw, Alaw
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
