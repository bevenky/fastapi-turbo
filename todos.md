# fastapi-rs — TODOs

Open work only. Shipped items are deleted after completion.

---

## FastAPI parity — critical gaps

Standard FastAPI code hits these and breaks or silently misbehaves.

### P0 — large

1. **File uploads** — `UploadFile` is a stub, `File(...)` marker not wired, multipart body parsing missing. Blocks the whole file-upload category of apps. ~250 LOC (Rust multipart parser + Python interface).

2. **`FileResponse` + `Range: bytes=...` header support** — no way to return a file with proper content-type, no video/resumable download support. ~180 LOC.

3. **`StaticFiles` mount serving files at scale** — mount helper exists but isn't wired. ~100 LOC.

### P1 — common features

4. **`SessionMiddleware`** — session cookie helper. ~80 LOC.

5. **`AuthenticationMiddleware`** — `request.auth` / `request.user` population. ~50 LOC (paired with `request.auth`/`request.user` properties on Request, ~15 LOC).

6. **ASGI middleware bridge** — Sentry, OpenTelemetry, Prometheus middleware can't be installed today (we only accept Tower middleware). ~150 LOC.

7. **`OAuth2ClientCredentials` + `OpenIdConnect`** security schemes. ~90 LOC.

8. **Pydantic v2 decorators on response_model**: `computed_field`, `field_serializer`, `model_serializer`, `model_validator` — not exercised in our response path. ~120 LOC.

9. **`request.stream()`** — read request body in chunks. ~30 LOC.

10. **Custom response headers on `accept(headers=...)`** — axum's `WebSocketUpgrade` doesn't expose them; needs hyper-level escape hatch. ~60 LOC.

### P2 — less common

11. TestClient WebSocket support (`client.websocket_connect(...)`).
12. `AsyncClient` / `ASGITransport` for async tests.
13. `HEAD` auto-handling from `GET` routes.
14. `OPTIONS` auto-generation for CORS preflight.
15. `405 Method Not Allowed` (currently returns 404 on wrong method).
16. Request size limits enforcement.
17. `redirect_slashes` parameter.
18. `dataclass` / `TypedDict` / `msgspec.Struct` as response models.
19. Per-route `servers` / `external_docs`.
20. `webhooks=` app parameter + OpenAPI webhooks section.
21. Multipart range responses (206 multipart).
22. Custom `APIRoute` via `route_class`.
23. `operation_id` uniqueness checks.

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
