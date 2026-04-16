# fastapi-rs — TODOs

Scope doc for planned work. Not starting implementation yet.

---

## Phase 1: WebSocket fast path for audio streaming — SHIPPED

**Goal:** Make fastapi-rs WebSocket performant enough that users can build real-time audio-streaming apps using standard FastAPI syntax, without needing to drop down to a separate Rust crate.

**Context:** Current WebSocket layer is ~57-58 μs/round-trip vs pure Rust Axum baseline of ~43 μs. The 12-15 μs gap is Python boundary cost. Critical bug: binary messages were being corrupted via `String::from_utf8_lossy`. For audio at 50 pkt/s (20 ms frames), per-packet framework overhead compounds across STT → LLM → TTS pipelines.

**Non-goal:** Do NOT add fixed-size batching (`receive_batch(n=5)`) — 5 × 20 ms = 100 ms latency, unacceptable for conversational AI. Target max batching window = 40 ms (2 packets) if we batch at all.

**Non-goal:** Do NOT add non-standard extensions (`ws.run_echo()`, `ws.on("message")`, etc.). Everything must work with standard FastAPI/Starlette WebSocket syntax so users can keep writing `await ws.receive_bytes()` loops.

---

### Raw Rust WebSocket performance (baselines)

From `benchmarks.md`:

| Library | p50 | msg/s |
|---|---|---|
| tokio-tungstenite (Axum default) | **41 μs** | 24,309 |
| fastwebsockets | **40 μs** | 22,435 |
| Pure Rust Axum echo (our bench) | **43-45 μs** | 23,211 |
| Go Gin (gorilla/websocket) | **47 μs** | 21,071 |
| Fastify (@fastify/websocket) | **48 μs** | 20,960 |
| **fastapi-rs (Phase 1)** | **58 μs** | **17,303** |

---

### Phase 1 items (A-G) — SHIPPED

#### A. Fix binary message corruption (BUG FIX) — DONE

`src/websocket.rs:133` previously did `String::from_utf8_lossy(&b).to_string()`, destroying any non-UTF8 data (protobuf, Opus audio, MessagePack). Replaced with typed `WsMessage` enum carrying `bytes::Bytes` end-to-end.

#### B. `ws.receive()` returning ASGI dict — DONE

Standard Starlette low-level API. Returns:
- `{"type": "websocket.receive", "text": str}` for text
- `{"type": "websocket.receive", "bytes": bytes}` for binary
- `{"type": "websocket.disconnect", "code": int, "reason": str}` on close

#### C. `WebSocketState` enum + `application_state` / `client_state` — DONE

Matches Starlette exactly (`CONNECTING=0`, `CONNECTED=1`, `DISCONNECTED=2`, `RESPONSE=3`). Atomic `u8` state tracking in Rust.

#### D. Zero-copy binary path — DONE

`axum::extract::ws::Message::Binary(Bytes)` flows through to Python as `PyBytes` via the typed `WsMessage` enum. No `Vec<u8>` intermediate, no UTF-8 validation.

#### E. Specialized cached awaitables — DONE

Three cached awaitables per `PyWebSocket` (dict / text / bytes). Each `await` reuses the same `pyclass` — no allocation per receive. Three distinct `__next__` implementations keep the hot path tight for each return type.

#### F. Starlette shim exports — DONE

`from starlette.websockets import WebSocketState, WebSocketDisconnect, WebSocket` all work via `python/fastapi_rs/compat/starlette_shim.py`.

#### G. State machine audit — DONE

Transitions: CONNECTING → (accept()) → CONNECTED → (close() or peer close) → DISCONNECTED. Atomic u8 enforces consistent reads across sync/async boundaries.

---

## Phase 1b — WebSocket Starlette compat — SHIPPED

**Status:** Fixed 7 of 8 gaps identified. 1 deferred (item #8 accept(headers) needs architectural work).

### Fixed

1. **`send_json()` compact separators** — `json.dumps(data, separators=(",", ":"), ensure_ascii=False)`. Matches Starlette byte-exact. HMAC-signed payloads now verify correctly.

2. **`close(reason=...)` propagated** — Rust `PyWebSocket::close(code, reason)` now accepts reason and builds a proper `CloseFrame`. Verified by peer receiving the reason string.

3. **`WebSocketDisconnect` carries real close code** — Rust awaitables now emit `WS_CLOSED:<code>:<reason>` in the `RuntimeError` message; Python layer parses and raises `WebSocketDisconnect(code=, reason=)` with the peer's actual values.

4. **`receive_json(mode)` / `send_json(mode)` validate** — raises `RuntimeError('The "mode" argument should be "text" or "binary".')` on invalid mode.

5. **HTTPConnection-like properties** — `ws.headers`, `ws.url`, `ws.base_url`, `ws.query_params`, `ws.path_params`, `ws.cookies`, `ws.client`, `ws.app`, `ws.scope` all implemented reading from the ASGI scope dict.

6. **State validation on send** — `send_text()` / `send_bytes()` / `send_json()` / `send()` raise `RuntimeError` if called before `accept()` or after `close()`. Matches Starlette's enforcement.

7. **`close()` truly awaits flush** — new `WriterCmd::Flush(cb::Sender)` queued after the Close frame. Writer task processes in order; `Flush` fires a crossbeam signal the Python-side `CloseAwaitable` blocks on. After `await ws.close()` returns, the close frame has reached the WS sink.

### Populated scope

The Rust route handler now passes `WsScopeInfo` to `handle_ws_connection()` containing path, raw_path, query_string, all headers, host, scheme (ws/wss), and path params. Python reads via `_ws.get_scope_dict()` on property access (lazy, cached).

### Phase 1c — deferred-upgrade `accept(subprotocol, headers)` — SHIPPED

Architectural change complete. The HTTP upgrade now defers until Python calls `accept()`:

1. Route handler creates pre-built mpsc + crossbeam channels and a `tokio::sync::oneshot<AcceptParams>`.
2. A pre-upgrade `PyWebSocket` is handed to the Python handler (spawned in a tokio task).
3. Python calls `await ws.accept(subprotocol="chat.v1")` — sends params via oneshot, blocks on `ready_rx` with GIL released.
4. Route handler receives params, applies `WebSocketUpgrade::protocols([...])` for subprotocol negotiation, then calls `on_upgrade(...)`.
5. `on_upgrade`'s callback wires reader/writer tasks to the pre-built channels and fires `ready_tx.send(())`.
6. Python's `accept()` unblocks. Send/receive flow normally.
7. If the handler never calls `accept()` within 30 s, the upgrade returns `500 Internal Server Error` cleanly (test verified).

**Subprotocol negotiation:** fully working. Client offers `["chat.v1", "chat.v2"]`, server calls `accept(subprotocol="chat.v1")`, 101 Switching Protocols response includes `Sec-WebSocket-Protocol: chat.v1`, and `ws.subprotocol` on the client reflects the negotiated value. Verified.

**Custom response headers** via `accept(headers=...)` — parameter is accepted and passed through but not yet emitted on the handshake response. Axum's `WebSocketUpgrade` API doesn't expose custom response headers without dropping to lower-level hyper. Semi-deferred; can be added with a small axum escape-hatch.

### Still deferred (strict-spec only, real code won't hit)

9. `websocket.connect` first-receive ASGI protocol — we skip the initial `{"type":"websocket.connect"}` frame.

10. `send_denial_response()` — WebSocket Denial Response extension (RFC 9455). Rare.

### Tests

- 19 WebSocket tests pass (9 pre-Phase 1b + 10 new Phase 1b compat tests)
- 332 non-WebSocket tests pass
- 351 total

---

## Phase 2 — `fastapi_rs.audio` helper modules

**Goal:** Enable building real-time voice-agent apps in pure Python FastAPI code with Rust-native performance on the per-frame hot operations.

**Approach:** Separate modules users opt into. None change the WebSocket API. Same pattern as `fastapi_rs.http` (reqwest) and `fastapi_rs.db` (psycopg3 helpers).

### Measured per-frame costs in typical voice-agent Python stacks

Per 20 ms audio frame at 48 kHz → 16 kHz mono:

| Operation | Typical Python implementation | Cost/frame |
|---|---|---|
| **WAV header creation** on output | `wave.open(BytesIO(), "wb")` + `writeframes()` per frame | 500-1000 μs |
| **Resampling** | soxr / resampy via numpy | 450-900 μs |
| **`asyncio.sleep` pacing** | `await asyncio.sleep(duration)` per frame | ±1-10 ms jitter |
| **Background audio mixing** | numpy `np.clip(a + b * volume, -32768, 32767)` | 62-100 μs |
| **G.711 μ-law / A-law codec** | `audioop.ulaw2lin` / `audioop.lin2ulaw` | 1 μs + resampling |
| **Recording (user + bot tracks)** | in-memory `bytearray` buffers | 10-100 μs, all RAM |
| **Opus codec** | user provides via external lib (e.g., `pyogg`) | varies |

### Python 3.13+ stdlib gap — `audioop` removed

`audioop` was deprecated in Python 3.11 and **removed from Python 3.13 stdlib**. Every telephony-oriented voice-agent Python codebase that does G.711 μ-law or A-law encoding via `import audioop` is broken on Python 3.13+ with `ModuleNotFoundError`. A native Rust G.711 codec is a direct replacement with no Python deps. This is the highest-blast-radius item in Phase 2.

### Phase 2 modules (proposed)

#### A. `fastapi_rs.audio.wav` — pre-built WAV header streamer

**Problem:** Python voice-agent code that emits WAV-wrapped audio frames calls `wave.open(BytesIO(), "wb")` on every frame. This recomputes an **identical** 44-byte RIFF header 50×/sec — pure waste.

**Solution:** Compute the RIFF header ONCE at session start; per-frame just prepend + update the 4-byte data-size field.

```python
from fastapi_rs.audio.wav import WavStreamer

streamer = WavStreamer(sample_rate=16000, channels=1, sample_width=2)

async def write_audio(ws, pcm: bytes):
    wav_bytes = streamer.wrap(pcm)  # ~2 μs (Rust: header copy + 4-byte update)
    await ws.send_bytes(wav_bytes)
```

**Cost today:** 500-1000 μs/frame → **~5 μs** with Rust helper. **~100× speedup.** At 50 fps × 100 concurrent streams: ~5 seconds of CPU/sec reclaimed.

**Complexity:** Low. ~30 lines Rust + 40 lines Python.

---

#### B. `fastapi_rs.audio.resample` — libspeexdsp or libsamplerate binding

**Problem:** Current Python resamplers go `bytes → numpy int16 → native-resample → numpy int16 → .tobytes()`. Two numpy allocations + one `.astype()` copy per frame.

**Solution:** Direct Rust resampler with `bytes → bytes` API, no numpy in the middle.

```python
from fastapi_rs.audio.resample import Resampler

r = Resampler(in_rate=48000, out_rate=16000, channels=1, quality="high")

audio_16k = r.process(audio_48k)  # Rust, ~100-200 μs
```

**Crate options:** `rubato` (pure Rust, solid quality), `speexdsp-sys` (FFI, fast), `libsamplerate-sys` (FFI, highest quality).

**Cost today:** 450-900 μs/frame → **~150-300 μs**. **~3× speedup** (amortizing the numpy copy overhead).

**Complexity:** Medium. ~80 lines Rust + ~60 Python.

---

#### C. `fastapi_rs.audio.pacer` — precise interval generator via tokio

**Problem:** `await asyncio.sleep(N)` has 1-10 ms jitter on typical OS schedulers. Over a 60-second call, drift compounds to ±100-600 ms.

**Solution:** Tokio's `tokio::time::interval` has ~100 μs precision (OS-limited). Expose as a Python async iterator.

```python
from fastapi_rs.audio.pacer import audio_pacer

async def send_loop(ws, frame_queue):
    async for _ in audio_pacer(interval_ms=20):  # wakes every 20 ms ±0.1 ms
        frame = await frame_queue.get()
        await ws.send_bytes(frame)
```

**Cost today:** 1-10 ms jitter → **<100 μs jitter**. Quality improvement, not raw speed.

**Complexity:** Low. ~40 lines. Needs tokio runtime → asyncio bridge (same pattern as `fastapi_rs.http`).

---

#### D. `fastapi_rs.audio.mixer` — SIMD PCM mixer

**Problem:** NumPy-based PCM mixing (`np.clip(a + b * volume, -32768, 32767)`) is vectorized but still has Python-loop-wrapping overhead (~62-100 μs/frame for 320 samples).

**Solution:** Saturating `i16 + i16` with SIMD (NEON on ARM, AVX2 on x86). Direct `bytes → bytes` API.

```python
from fastapi_rs.audio.mixer import mix_saturating

mixed = mix_saturating(voice_pcm, background_pcm, volume=0.3)  # ~5-10 μs
```

**Cost today:** 62-100 μs/frame → **~5-10 μs**. **~10× speedup.**

**Complexity:** Low. ~50 lines Rust (`wide` crate or `std::simd`) + 30 Python.

---

#### E. `fastapi_rs.audio.codec` — G.711 μ-law / A-law **[CRITICAL for Python 3.13+]**

**Problem:** Python voice-agent code that does G.711 encode/decode via `import audioop` fails on Python 3.13+ with `ModuleNotFoundError: No module named 'audioop'`. Every telephony platform (Twilio, Plivo, Telnyx, Vonage) uses G.711 μ-law (US) or A-law (EU) over its media WebSockets at 8 kHz. This is the hot path — 100 codec calls/sec per call.

**Solution:** Pure-Rust G.711 codec with 256-byte lookup tables. Same table lookups `audioop` used (both just implement ITU-T G.711). ~100 lines Rust total.

```python
from fastapi_rs.audio.codec import Mulaw, Alaw

# 8-bit μ-law (160 bytes / 20 ms @ 8 kHz) → 16-bit PCM (320 bytes)
pcm = Mulaw.decode(ulaw_bytes)    # ~1 μs (table lookup)

# 16-bit PCM → 8-bit μ-law
ulaw = Mulaw.encode(pcm_bytes)    # ~1 μs

# Same API for A-law (European telephony)
pcm = Alaw.decode(alaw_bytes)
alaw = Alaw.encode(pcm_bytes)
```

**`audioop` shim for drop-in compat:**

Users can install a `sys.modules` shim at app startup so any library that does `import audioop` works transparently on Python 3.13+:

```python
import fastapi_rs.audio.codec as _codec
import sys, types
_shim = types.ModuleType("audioop")
_shim.ulaw2lin = lambda b, w: _codec.Mulaw.decode(b)
_shim.lin2ulaw = lambda b, w: _codec.Mulaw.encode(b)
_shim.alaw2lin = lambda b, w: _codec.Alaw.decode(b)
_shim.lin2alaw = lambda b, w: _codec.Alaw.encode(b)
sys.modules["audioop"] = _shim
```

Downstream libraries that still `import audioop` then work unchanged.

**Cost:** broken on Python 3.13+ without this → **~1 μs/frame** with Rust codec (faster than C `audioop` because no Python boundary on the loop path).

**Complexity:** Low. ~100 lines Rust + ~40 Python.

---

#### F. `fastapi_rs.audio.record` — streaming WAV/Opus recorder

**Problem:** Common Python voice-agent patterns buffer the entire call in memory (`bytearray` or numpy array), then write to disk at call end. For long calls, memory grows unbounded. 1 hour of 16 kHz mono PCM = ~115 MB per call. No built-in streaming-to-disk.

**Solution:** Streaming recorder that writes frames as they arrive:

```python
from fastapi_rs.audio.record import Recorder

# Streaming WAV — header written on first frame, data appended, finalized on close
rec = Recorder.wav("call.wav", sample_rate=16000, channels=2)

# Dual-track (user L, bot R) — writes interleaved stereo frames
rec.write_stereo(user_pcm_frame, bot_pcm_frame)  # ~5 μs

await rec.close()  # finalizes WAV header with final data size

# Or Opus (compressed) for long-term storage
rec = Recorder.opus("call.opus", sample_rate=16000, channels=2, bitrate=64000)
rec.write_stereo(user_pcm_frame, bot_pcm_frame)  # ~500 μs (opus encode)
await rec.close()
```

**Cost today:**
- Memory: **~115 MB per hour of mono 16 kHz** buffered in RAM
- Finalization: O(n) mix/interleave at call end (~10-100 ms for a 1-hour call)
- No disk streaming at all

**With `fastapi_rs.audio.record`:**
- Memory: **<64 KB bounded buffer** (write-through)
- Per frame: ~5 μs for WAV, ~500 μs for Opus encode
- Finalization: write WAV header length field (~1 μs), or close Opus stream

**Crates:** `hound` for WAV (pure Rust, streaming), `opus` for Opus (libopus FFI).

**Complexity:** Medium. ~200 lines Rust + ~80 Python.

---

### Phase 2 priority order (updated)

Ranked by blast radius:

1. **E. `.codec` (G.711 μ-law / A-law)** — **CRITICAL.** Unblocks Python 3.13+ for every telephony-oriented voice-agent app using `audioop`. Smallest module (~100 LOC Rust), biggest user-impact.
2. **A. `.wav` (WavStreamer)** — 100× speedup on WAV header generation, trivial LOC.
3. **F. `.record` (streaming recorder)** — Memory fix + missing-feature for long-call recording.
4. **B. `.resample`** — 3× speedup on rate conversion, medium LOC.
5. **C. `.pacer` (tokio interval)** — precision win for output pacing, low LOC.
6. **D. `.mixer` (SIMD saturating add)** — 10× speedup on background-audio mixing, low LOC.

**Explicitly dropped:** Rust ONNX Silero VAD wrapper. Out of scope — ONNX inference itself is the real cost, a Rust wrapper only shaves the Python boundary overhead. Users can plug any VAD via existing Python async interfaces.

**Recommendation:** Ship E (codec) first — it unblocks Python 3.13+ for all telephony users TODAY. A (wav) + F (record) are complementary voice-agent primitives. B/C/D when needed.

---

## Phase 3 — rejected designs (documented so we don't re-litigate)

- `ws.run_echo()` / `ws.run_forward()` / `ws.on("message", handler)` — non-standard extensions that break the FastAPI mental model. Users should write the standard `while True: await ws.receive_bytes()` loop; we make it fast.
- Fixed-size message batching (`receive_batch(n=5)`) — adds 100 ms latency per 5 frames, destroys conversational AI. Rejected.
- Full SIP/RTP stack — that's a separate project. Out of scope for a general web framework.

---

## Phase 4 — FastAPI parity: remaining gaps from full audit

Comprehensive audit against upstream FastAPI + Starlette found ~80 missing or incomplete features. Grouped by severity. Most real apps hit only the CRITICAL list; HIGH+ mainly affect corner cases or specialized workloads.

### CRITICAL — standard FastAPI code will break

These will either raise `AttributeError`/`TypeError` or produce wrong output silently.

#### 4.1 `FastAPI.__init__` missing params

| Param | Upstream default | Impact | LOC |
|---|---|---|---|
| `debug: bool = False` | Stored; used for error-page verbosity | Debug tracebacks not returned on 500 | ~5 |
| `responses: dict` | App-wide default response schemas | Can't set app-level 404/500 shapes in OpenAPI | ~15 |
| `default_response_class: type[Response]` | Sets app-wide default | Can't make ORJSONResponse the app default | ~10 |
| `swagger_ui_oauth2_redirect_url: str` | Custom OAuth2 redirect path | OAuth2 apps with custom paths break | ~5 |
| `separate_input_output_schemas: bool = True` | Separates req/resp schemas | OpenAPI SDK generators emit wrong types | ~50 |

#### 4.2 `APIRouter.__init__` missing params

| Param | Impact | LOC |
|---|---|---|
| `default_response_class` | Can't override response class per router | ~8 |
| `responses` | Router-level response schemas missing from OpenAPI | ~15 |
| `route_class` | Can't use custom `APIRoute` subclass per router | ~20 |
| `default` (404 handler) | Can't customize 404 per router | ~10 |

#### 4.3 Route decorator

| Param | Impact | LOC |
|---|---|---|
| `response_model_by_alias: bool = True` | Responses don't use `Field(alias=...)` — breaks all APIs using aliased fields | ~20 |

#### 4.4 Response serialization / Pydantic integration

- **`response_model` filtering is incomplete** — current `_apply_response_model` validates via Pydantic but doesn't honor `by_alias`. Nested models may not $ref correctly.
- **Missing `Field(alias=..., serialization_alias=...)` support** — users can declare aliased Pydantic models but output ignores the alias on serialization.
- **`computed_field`, `field_serializer`, `model_serializer`, `model_validator`** — Pydantic v2 decorators not exercised in our response path.
- Estimated fix: ~60 LOC for alias support, ~120 for full Pydantic v2 decorator plumbing.

#### 4.5 File handling (major gap)

- **`UploadFile` is a stub** — doesn't actually receive/buffer multipart data. File uploads don't work.
- **`File(...)` marker not wired** — params decorated with `File()` aren't handled during introspection.
- **`FileResponse` missing** — no way to return a file with Content-Type + Range support.
- **`Range: bytes=...` request handling missing** — video streaming / resumable downloads don't work.
- **`StaticFiles` not functional** — mount helper exists but doesn't serve files at scale.
- Estimated fix: **~700 LOC** including Rust multipart parser (~200), FileResponse with Range (~100), StaticFiles (~100), UploadFile proper interface (~100), body-size limits (~50), and testing (~150).

#### 4.6 Request enhancements

| Gap | Impact | LOC |
|---|---|---|
| `request.stream()` missing | Can't read request body in chunks | ~30 |
| `request.auth` / `request.user` missing | Security middleware can't populate identity | ~15 |
| `request.form()` multipart missing | Multipart forms don't parse (blocked by #4.5) | 0 (after #4.5) |

### HIGH — common features affecting many users

- **`ORJSONResponse`** listed as supported but not backed by orjson (falls back to `json`). ~15 LOC.
- **`StreamingResponse` + `BackgroundTask`** — verify background runs after stream closes.
- **`@app.middleware("http")`** decorator — already shipped; verify Starlette-compat ordering semantics.
- **ASGI middleware support** — explicitly deferred; users with Sentry / OpenTelemetry / Prometheus middleware can't install them. ~150 LOC.
- **`SessionMiddleware`** — no session cookie helper. ~80 LOC.
- **`AuthenticationMiddleware`** — no built-in auth user population. ~50 LOC.
- **OAuth2ClientCredentials, OpenIdConnect** — missing security schemes. ~90 LOC combined.
- **Discriminated Union response models** — untested, likely wrong schema.

### MEDIUM — less common

- `webhooks` parameter + OpenAPI webhooks section.
- `servers` / `external_docs` at route level (currently only app level).
- `Form(..., Depends())` embedded form validation.
- TestClient WebSocket support (`ws.websocket_connect()`).
- `AsyncClient` / `ASGITransport` integration for async tests.
- `dataclass` / `TypedDict` / `msgspec.Struct` as response models (Pydantic-only today).
- `HEAD` auto-handling from `GET` routes.
- `OPTIONS` auto-generation for CORS preflight.
- `405 Method Not Allowed` responses (currently may return 404).
- Request size limits enforcement.
- `redirect_slashes` parameter.
- `lifespan` ordering edge cases (routers with own lifespans).

### LOW — edge cases / rare

- `openapi_prefix` (deprecated upstream; alias to `root_path`).
- `contact` dict field validation.
- `generate_unique_id_function` at app level (per-route works).
- Custom 404 / 500 HTML pages (can use `exception_handler`).
- `APIRoute` subclassing / `route_class` param.
- `operation_id` uniqueness checks.
- Multipart range responses (206 multipart).
- Webhooks extension.

### Phase 4 priority

Biggest blast radius first, grouped by effort:

1. **response_model_by_alias + Field(alias=)** — breaks every app using Pydantic aliases. ~80 LOC.
2. **default_response_class (app + router + route)** — many apps want ORJSONResponse as default. ~30 LOC.
3. **ORJSONResponse actually using orjson** — ~15 LOC.
4. **`responses` parameter at app level** — app-wide response shapes. ~15 LOC.
5. **File uploads + FileResponse + Range** — unblocks an entire category of apps. ~700 LOC (biggest item).
6. **Session / Authentication middleware** — common auth patterns. ~130 LOC.
7. **ASGI middleware bridge** — unblocks Sentry / OpenTelemetry. ~150 LOC.
8. **OAuth2ClientCredentials, OpenIdConnect** — common OAuth flows. ~90 LOC.
9. All MEDIUM items — pick up as users hit them.

Total Phase 4 scope: ~1200 LOC excluding Phase 2 audio modules.
