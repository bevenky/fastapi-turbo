# fastapi-turbo — TODOs

Open work only. Shipped items are deleted after completion.

---

## FastAPI parity — critical gaps

Standard FastAPI code hits these and breaks or silently misbehaves.

### P0 — large

1. **File uploads** — `UploadFile` is a stub, `File(...)` marker not wired, multipart body parsing missing. Blocks the whole file-upload category of apps. ~250 LOC (Rust multipart parser + Python interface).

2. **`FileResponse` + `Range: bytes=...` header support** — no way to return a file with proper content-type, no video/resumable download support. ~180 LOC.

3. **`StaticFiles` mount serving files at scale** — mount helper exists but isn't wired. ~100 LOC.

### P1 — common features

4. **ASGI middleware bridge** — Sentry, OpenTelemetry, Prometheus middleware can't be installed today (we only accept Tower middleware). ~150 LOC.

5. **Custom response headers on `accept(headers=...)`** — axum's `WebSocketUpgrade` doesn't expose them; needs hyper-level escape hatch. ~60 LOC.

### P2 — less common

6. TestClient WebSocket support (`client.websocket_connect(...)`).
7. `AsyncClient` / `ASGITransport` for async tests.
8. `HEAD` auto-handling from `GET` routes.
9. `OPTIONS` auto-generation for CORS preflight.
10. `405 Method Not Allowed` (currently returns 404 on wrong method).
11. Request size limits enforcement.
12. `redirect_slashes` parameter.
13. `dataclass` / `TypedDict` / `msgspec.Struct` as response models.
14. Per-route `servers` / `external_docs`.
15. `webhooks=` app parameter + OpenAPI webhooks section.
16. Multipart range responses (206 multipart).
17. Custom `APIRoute` via `route_class`.
18. `operation_id` uniqueness checks.

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
