# fastapi-rs — TODOs

Scope doc for planned work. Not starting implementation yet.

---

## Phase 1: WebSocket fast path for audio streaming

**Goal:** Make fastapi-rs WebSocket performant enough that someone can build agent-transport-class audio streaming apps using standard FastAPI syntax, without needing a separate Rust crate.

**Context:** Current WebSocket layer is ~57 μs/round-trip vs pure Rust Axum baseline of ~45 μs. The 12 μs gap is Python boundary cost. Critical bug: binary messages are corrupted via `String::from_utf8_lossy`. For audio at 50 pkt/s (20ms frames), per-packet framework overhead compounds across STT→LLM→TTS pipelines.

**Non-goal:** Do NOT add fixed-size batching (`receive_batch(n=5)`) — 5 × 20ms = 100ms latency, unacceptable for conversational AI. Target max batching window = 40 ms (2 packets) if we batch at all.

**Non-goal:** Do NOT add non-FastAPI-standard extensions (`ws.run_echo()`, `ws.on("message")`, etc.). Everything must work with standard FastAPI WebSocket syntax so users can keep writing `await ws.receive_bytes()` loops.

---

### Raw Rust WebSocket performance (baselines)

From `benchmarks.md`:

| Library | p50 | msg/s |
|---|---|---|
| tokio-tungstenite (Axum default) | **41 μs** | 24,309 |
| fastwebsockets | **40 μs** | 22,435 |
| Pure Rust Axum echo (our bench) | **45 μs** | 21,766 |
| Go gorilla/websocket | 44 μs | 22,030 |
| Node ws (Fastify) | 44 μs | 22,119 |
| **fastapi-rs (today, with Python handler)** | **57 μs** | **17,339** |

Target after Phase 1: **~48-50 μs**, within 5 μs of pure Axum baseline.

---

### Phase 1 items (A-E + Pipecat-compat items F-G)

#### A. Fix binary message corruption (BUG FIX)

**File:** `src/websocket.rs:133`

Current code:
```rust
Message::Binary(b) => String::from_utf8_lossy(&b).to_string(),
```

This destroys any non-UTF8 data — protobuf, Opus audio, MessagePack all break. Users can't write binary WebSocket apps today.

**Fix:**
- Replace `cb::Receiver<String>` with `cb::Receiver<WsMessage>`:
  ```rust
  enum WsMessage {
      Text(String),
      Binary(Vec<u8>),    // later: Arc<Bytes> for zero-copy
      Close { code: u16, reason: String },
  }
  ```
- `receive_bytes()` returns actual bytes, not corrupted UTF-8 string.
- `receive_text()` stays as-is for text frames; errors cleanly on binary frames received as text.

Estimated win: correctness (fixes actual bug) + ~2 μs (skip `from_utf8_lossy` copy).

---

#### B. Add `ws.receive()` returning Starlette-compatible dict

**Critical for Pipecat compatibility.** Pipecat's hot path in `pipecat/transports/websocket/fastapi.py:94-102`:

```python
async def __anext__(self) -> bytes | str:
    message = await self._websocket.receive()      # ← Pipecat calls this
    if message["type"] == "websocket.disconnect":
        raise StopAsyncIteration
    if "bytes" in message and message["bytes"] is not None:
        return message["bytes"]
    if "text" in message and message["text"] is not None:
        return message["text"]
```

We don't implement `ws.receive()` → dict today. Only `receive_text()` / `receive_bytes()`. Pipecat can't use our WS as-is.

**Add:**
```python
async def receive(self) -> dict:
    """Low-level receive matching Starlette's ASGI receive protocol.
    Returns {"type": "websocket.receive", "bytes": ...} or
            {"type": "websocket.receive", "text": ...} or
            {"type": "websocket.disconnect", "code": ...}
    """
```

Enables `await ws.receive()` → dict (standard ASGI), without breaking existing `receive_text()` / `receive_bytes()` callers.

---

#### C. Add `WebSocketState` enum + `application_state` / `client_state` properties

**Also critical for Pipecat.** From `pipecat/transports/websocket/fastapi.py:45, 187`:

```python
from starlette.websockets import WebSocketState
...
if self._websocket.application_state != WebSocketState.DISCONNECTED:
    # safe to send
```

We don't have `WebSocketState` or these state properties today. Pipecat's state-check calls would AttributeError.

**Add:**
```python
# python/fastapi_rs/websockets.py
class WebSocketState(enum.Enum):
    CONNECTING = 0
    CONNECTED = 1
    DISCONNECTED = 2
    RESPONSE = 3

class WebSocket:
    @property
    def application_state(self) -> WebSocketState: ...
    @property
    def client_state(self) -> WebSocketState: ...
```

Also export from our shim so `from starlette.websockets import WebSocketState` works.

---

#### D. Zero-copy binary path (Bytes → PyBytes)

**File:** `src/websocket.rs`

Today a binary message does: Axum `Bytes` → `Vec<u8>` → `String::from_utf8_lossy` → `to_string` → crossbeam send → `PyBytes` allocation.

**Change:**
- Keep as `Bytes` (Arc'd in axum) all the way to the PyO3 boundary.
- Only allocate `PyBytes` when Python actually calls `receive_bytes()`.
- Skip `Vec<u8>` intermediate entirely.

Estimated win: ~2 μs per message (one fewer allocation per packet).

---

#### E. Cached `ChannelAwaitable`

**File:** `src/websocket.rs:75`

Today every `await ws.receive_text_async()` creates a new `ChannelAwaitable` pyclass instance. Reused awaitable shaves ~1 μs per await.

**Change:**
- Store one `Py<ChannelAwaitable>` on `PyWebSocket` at construction.
- Return clone of that same handle each time.
- Safe because a single WS connection only has one reader at a time.

Estimated win: ~1 μs per message.

---

#### F. Starlette shim exports for WebSocket

Our `compat/starlette_shim.py` needs to expose:
- `starlette.websockets.WebSocketState` (enum)
- `starlette.websockets.WebSocketDisconnect` (already exposed? verify)
- `starlette.websockets.WebSocket` (already exposed)

Without this, `from starlette.websockets import WebSocketState` (which Pipecat does) fails when user installs fastapi-rs.

---

#### G. Audit `application_state` transitions

Match Starlette's state machine exactly:
- `CONNECTING` initially
- `CONNECTED` after `accept()`
- `DISCONNECTED` after close from either side
- `RESPONSE` during close-send (rarely used)

Required so Pipecat's `self._websocket.application_state != WebSocketState.DISCONNECTED` check works reliably.

---

### Phase 1 expected impact

After A-G:

**Correctness wins:**
- Binary data no longer corrupted (critical bug fix).
- Pipecat can run on fastapi-rs unchanged (drop-in compat).

**Performance wins:**
- `await ws.receive_bytes()`: 57 μs → ~50 μs (within 5 μs of pure Axum)
- Binary throughput: 17 K msg/s → ~20 K msg/s (matches Go/Fastify)

**API compatibility:** 100% FastAPI + Starlette standard. No new APIs users have to learn.

---

### What Phase 1 explicitly does NOT do

- No `ws.run_echo()` / `ws.iter_batched()` / `ws.on(...)` — these would add non-standard APIs and don't help the standard FastAPI pattern.
- No fixed-size batching (would add audio latency).
- No new audio helpers (codecs, resampling, VAD) — those are Phase 2 as separate modules (`fastapi_rs.audio.*`), not WebSocket changes.
- No WAV-header optimization — that's inside Pipecat's serializer, not our code. Documenting it so users know.

---

## Pipecat compatibility audit (reference data)

Collected from Pipecat 0.0.108 at `/Users/venky/tech/agent-transport/.venv/lib/python3.13/site-packages/pipecat/`.

### What Pipecat imports from FastAPI/Starlette

Very little. Pipecat is **mostly framework-agnostic** — it uses standard ASGI WebSocket methods.

```python
# pipecat/transports/websocket/fastapi.py:44-50
from fastapi import WebSocket
from starlette.websockets import WebSocketState
```

Plus `from pydantic import BaseModel` for callback signatures (not validation).

**What Pipecat does NOT use from FastAPI:**
- No `Depends()`, no Pydantic validation, no middleware, no routing decorators, no Request/Response classes, no streaming responses.
- No dependence on uvicorn (users wire that up themselves).

**User-side pattern:**
```python
from fastapi import FastAPI, WebSocket
import uvicorn

app = FastAPI()

@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    transport = FastAPIWebsocketTransport(websocket, params)
    await run_pipeline(transport)

uvicorn.run(app, host="0.0.0.0", port=8000)
```

Replacing `from fastapi import FastAPI, WebSocket` with `from fastapi_rs import FastAPI, WebSocket` should Just Work once Phase 1 ships.

### Pipecat's audio hot path (per-frame work)

**Input (`pipecat/transports/websocket/fastapi.py:299-323`):**
```
await ws.receive()                    ← needs our receive() dict (item B)
→ serializer.deserialize(msg.bytes)   ← Pipecat's protobuf (~50-100 μs)
→ create InputAudioRawFrame (~5 μs Python)
→ audio_in_queue.put()                ← asyncio.Queue (~10 μs)
→ downstream pipeline
```

**Output (`pipecat/transports/websocket/fastapi.py:449-527`):**
```
OutputAudioRawFrame
→ [if add_wav_header:]
    wave.open(BytesIO) + writeframes()   ← ~500-1000 μs per frame (Pipecat bottleneck, not ours)
→ serializer.serialize(frame)            ← Pipecat's protobuf (~50-100 μs)
→ [if fixed_audio_packet_size:]
    buffer.extend() + slice + del          ← ~100 μs per send
→ await ws.send_bytes(chunk)             ← our code — we want this FAST
→ await asyncio.sleep(send_interval)     ← intentional, simulates playback timing
```

### Bottlenecks fastapi-rs can eliminate (reachable via our code)

| Bottleneck | Where | Current cost/frame | After Phase 1 |
|---|---|---|---|
| Binary UTF-8 corruption | `src/websocket.rs` | BROKEN | Fixed |
| Bytes alloc on every receive | `src/websocket.rs` | ~2 μs | ~0.5 μs (zero-copy) |
| `ChannelAwaitable` per-await alloc | `src/websocket.rs` | ~1 μs | 0 μs (cached) |
| Missing `receive()` dict | `websockets.py` | N/A (breaks Pipecat) | Works |
| Missing `WebSocketState` | `websockets.py` | N/A (breaks Pipecat) | Works |
| `send_bytes` PyO3 marshaling | `src/websocket.rs` | ~3 μs | ~2 μs (accept `&PyBytes`) |

**Total per-frame savings for Pipecat users: ~5-8 μs.**

At 50 pkt/s bidirectional audio: ~0.5-0.8 ms/sec CPU saved per stream. On a server handling 100 concurrent streams: ~50-80 ms/sec reclaimed — meaningful for dense deployments.

### Bottlenecks outside fastapi-rs (Pipecat's own code)

These are NOT in Phase 1 scope. Documented for awareness — users may still want to PR them upstream to Pipecat.

1. **WAV header creation per frame** (`fastapi.py:467-479`): `with wave.open(BytesIO(), "wb")` allocates a WAV writer object every 20 ms. This is ~500-1000 μs per frame when `add_wav_header=True`. Pre-allocate once per stream.
2. **Protobuf serialization per frame** (~50-100 μs): Pipecat's choice. Could use MessagePack or raw binary for simple audio frames.
3. **`asyncio.Queue` hop** (~10-50 μs per frame): Pipecat's internal architecture. Could be inlined for latency-critical paths.
4. **`asyncio.sleep` playback pacing**: intentional — simulates real-time audio output rate, don't remove.

---

## Phase 1b — WebSocket Starlette compatibility gaps (from audit)

**Status:** Phase 1 shipped (binary fix, receive() dict, WebSocketState). A deeper audit against `/Users/venky/tech/agent-transport/.venv/lib/python3.13/site-packages/starlette/websockets.py` found 11 additional breaking gaps. Phase 1b fixes the real-world ones (Pipecat compat). Low-risk spec edge cases deferred.

### Must fix (standard code will break otherwise)

1. **`send_json()` separators mismatch** — we emit `{"key": "value"}`, Starlette emits `{"key":"value"}`. Signature-sensitive protocols (HMAC-signed payloads, exact byte count) will fail. Fix: `json.dumps(data, separators=(",", ":"), ensure_ascii=False)`. ~2 LOC.

2. **`close()` drops `reason`** — `ws.close(code=3000, reason="bye")` sends empty reason. Rust `PyWebSocket::close` hardcodes `reason: "".into()`. Fix: plumb reason through PyO3 → CloseFrame. ~5 LOC.

3. **`WebSocketDisconnect` hardcodes `code=1000`** — when the peer closes with code 1011 (Internal Error), our `receive_text()`/`receive_bytes()` still raise `WebSocketDisconnect(code=1000)`. Fix: propagate actual Close code from `WsMessage::Close { code, ... }` up to the Python exception. ~20 LOC (needs the close code to survive the RuntimeError path).

4. **`receive_json(mode)` / `send_json(mode)` accept anything** — Starlette raises `RuntimeError` on unknown mode; we silently treat non-"text" as "binary". Fix: validate `mode in ("text","binary")`. ~4 LOC.

5. **Missing `app`, `headers`, `url`, `query_params`, `path_params`, `cookies`, `client` properties** — Starlette's `WebSocket` extends `HTTPConnection`, so these are inherited. Pipecat uses several (`ws.client`, `ws.headers`). Fix: add these properties reading from `self._scope`. ~60 LOC.

### Should fix (Pipecat-adjacent edge cases)

6. **State validation on send/receive** — Starlette raises `RuntimeError` on `send_text()` before `accept()`; we silently call. Fix: pre-condition check `application_state == CONNECTED`. ~15 LOC total.

7. **`close()` not truly awaited** — we queue the close frame to a tokio mpsc but don't await flush. Code that closes then immediately exits may lose the close frame. Fix: use `tokio::sync::oneshot` to signal write completion. ~25 LOC.

8. **`accept()` ignores `headers`** — our Rust `accept()` discards custom headers. For Pipecat `Sec-WebSocket-Protocol` negotiation this breaks. Fix: pass headers to `axum::extract::ws::WebSocket::on_upgrade` (may require restructuring the upgrade path). ~40 LOC.

### Deferred (strict spec-only, won't break real code)

9. `websocket.connect` first-receive handling — ASGI spec says receive() emits `{"type":"websocket.connect"}` on first call. Our Rust bridge skips this. Most user code never calls `receive()` before `accept()`, so impact is low. Defer.

10. Message type validation inside `send()` / `receive()` dispatch — Starlette validates message type transitions. We're looser. Defer.

11. `send_denial_response()` — WebSocket Denial Response extension. Rare use case. Defer.

**Total Phase 1b LOC estimate:** ~170 lines across Rust+Python, mostly glue.

---

## Phase 2 — `fastapi_rs.audio` helper modules

**Goal:** Let users build Pipecat-class voice-agent apps in pure Python FastAPI code, with Rust-native performance on the per-frame hot operations.

**Approach:** Separate modules users opt into. None change the WebSocket API. Same pattern as `fastapi_rs.http` (reqwest) and `fastapi_rs.db` (psycopg3 helpers).

### Audit of Pipecat's per-frame costs (from `/Users/venky/tech/agent-transport/.venv/lib/python3.13/site-packages/pipecat/`)

Measured per 20 ms audio frame at 48kHz→16kHz mono:

| Operation | File | Current cost/frame | Frequency |
|---|---|---|---|
| **WAV header creation** (when `add_wav_header=True`) | `transports/websocket/fastapi.py:467-479` | **500-1000 μs** | Every output frame (50/s) |
| **Resampling** (soxr) | `audio/resamplers/soxr_stream_resampler.py:83-101` | **450-900 μs** | Every frame needing rate conversion |
| **`asyncio.sleep` pacing** | `transports/websocket/fastapi.py:518-528` | ~20 ms jitter | Every output frame |
| **Background audio mixing** (NumPy) | `audio/mixers/soundfile_mixer.py:170-195` | **62-100 μs** | Every output frame when mixer on |
| **Silero VAD** (ONNX inference) | `audio/vad/silero.py:199-226` | **5-15 ms** | Every 512 samples (~32ms) |
| **Opus/G.711 codec** | N/A in Pipecat — user layer | N/A | N/A |

### Phase 2 modules (proposed)

#### A. `fastapi_rs.audio.wav` — pre-built WAV header streamer

**Problem:** Pipecat's `wave.open(BytesIO(), "wb")` per frame recomputes an **identical** 44-byte RIFF header every time. 50 times/second of pure waste.

**Solution:** Compute the RIFF header ONCE at session start, then just prepend + update the 4-byte data-size field per frame.

```python
# Rust-backed, zero allocation per frame after init
from fastapi_rs.audio.wav import WavStreamer

streamer = WavStreamer(sample_rate=16000, channels=1, sample_width=2)

async def write_audio(ws, pcm: bytes):
    wav_bytes = streamer.wrap(pcm)  # ~2μs (Rust: header copy + 4-byte update)
    await ws.send_bytes(wav_bytes)
```

**Pipecat integration:** Pipecat users set `add_wav_header=False` and wrap themselves, OR we provide a drop-in replacement serializer.

**Cost today:** 500-1000 μs/frame → **~5 μs** with Rust helper. **~100x speedup.**

**Impact:** At 50 fps: ~40 ms/sec CPU reclaimed per stream. For 100 concurrent streams: 4 seconds of CPU/sec saved (i.e., 4 full cores at 100% utilization freed).

**Complexity:** Low. ~30 lines of Rust + 40 lines of Python binding.

---

#### B. `fastapi_rs.audio.resample` — libspeexdsp or libsamplerate binding

**Problem:** Pipecat's `SOXRStreamAudioResampler.resample()` (via the `soxr` crate) already calls native code, but goes through: `bytes → numpy int16 → soxr → numpy int16 → .tobytes()`. That's 2 numpy allocations + 1 `.astype()` copy per frame.

**Solution:** Direct Rust resampler with `bytes → bytes` API (no numpy in the middle).

```python
from fastapi_rs.audio.resample import Resampler

r = Resampler(in_rate=48000, out_rate=16000, channels=1, quality="high")

async def receive_audio(ws):
    audio_48k = await ws.receive_bytes()  # raw PCM
    audio_16k = r.process(audio_48k)       # Rust, ~100-200 μs
```

**Pipecat integration:** Subclass `BaseAudioResampler` to wrap our Rust Resampler. User passes it via `SOXRStreamAudioResamplerFactory` or similar. No Pipecat PR needed.

**Cost today:** 450-900 μs/frame → **~150-300 μs** with Rust-direct (skip numpy hops). **~3x speedup.**

**Complexity:** Medium. Depends which crate — `speexdsp` (pure Rust, fast), `libsamplerate` (FFI, higher quality). ~80 lines Rust + ~60 Python.

---

#### C. `fastapi_rs.audio.pacer` — precise interval generator via tokio

**Problem:** `await asyncio.sleep(N)` has 1-10 ms jitter on typical OS schedulers. Over a 60-second call, drift compounds to ±100-600 ms. Pipecat's `_write_audio_sleep()` tries to self-correct (`_next_send_time += interval`) but is still asyncio-bound.

**Solution:** Tokio's `tokio::time::interval` has ~100 μs precision (OS-limited). Expose as a Python async iterator.

```python
from fastapi_rs.audio.pacer import audio_pacer

async def send_loop(ws, frame_queue):
    async for _ in audio_pacer(interval_ms=20):  # wakes every 20ms ±0.1ms
        frame = await frame_queue.get()
        await ws.send_bytes(frame)
```

**Pipecat integration:** Pipecat would need to accept a pacer factory via transport params. Currently hard-codes `asyncio.sleep`. So: either they add a hook (upstream PR) OR we provide an alternate transport class `PacedWebSocketTransport` users opt into.

**Cost today:** 1-10 ms jitter → **<100 μs jitter**. Quality improvement, not raw-speed.

**Complexity:** Low. ~40 lines total. But requires tokio runtime bridge back to asyncio — we already have the patterns from `fastapi_rs.http`.

---

#### D. `fastapi_rs.audio.mixer` — SIMD PCM mixer

**Problem:** Pipecat's `_mix_with_sound()` does `np.clip(audio_np + sound_np * volume, -32768, 32767)`. NumPy is vectorized but still has Python loop overhead (~62-100 μs/frame for 320 samples).

**Solution:** Saturating `i16 + i16` with SIMD (NEON on ARM, AVX2 on x86). Direct `bytes → bytes` API.

```python
from fastapi_rs.audio.mixer import mix_saturating

mixed = mix_saturating(voice_pcm, background_pcm, volume=0.3)  # ~5-10 μs
```

**Pipecat integration:** Drop-in replacement for `_mix_with_sound`. Users subclass `SoundfileMixer` to use our mixer.

**Cost today:** 62-100 μs/frame → **~5-10 μs**. **~10x speedup.**

**Complexity:** Low. ~50 lines of Rust (use `wide` or `std::simd`) + 30 Python.

---

#### E. `fastapi_rs.audio.vad` — Rust ONNX Silero VAD binding

**Problem:** Pipecat's Silero VAD runs `self._model(audio_float32, sample_rate)` (Python onnxruntime), taking 5-15 ms per 512-sample analysis. This is the BIGGEST per-call cost in the audio path.

**Solution:** Use the `ort` (ONNX Runtime) Rust crate. Same underlying model/ONNX runtime, but without Python GIL contention and no numpy → float32 allocation per call.

```python
from fastapi_rs.audio.vad import SileroVAD

vad = SileroVAD(sample_rate=16000, model_path="silero_vad.onnx")
confidence = vad.process(pcm_bytes)  # ~3-8 ms (vs 5-15 ms Python-side)
```

**Pipecat integration:** Subclass `VADAnalyzer`. Users pass our VAD via `SileroVADAnalyzerFactory`. Modest Pipecat PR needed, or users wire it manually.

**Cost today:** 5-15 ms/call → **3-8 ms**. **~2x speedup.**

**Caveat:** ONNX inference itself is the bottleneck — Rust wrapper just removes the Python overhead. Not a massive win unless we also support quantized models.

**Complexity:** Medium-high. ~200 lines Rust (model loading, state management) + 60 Python. ONNX model file shipped separately.

---

### Phase 2 priority order

Biggest win per LOC: **A (WAV) → D (Mixer) → C (Pacer) → B (Resample) → E (VAD)**.

Recommendation: Ship A (wav) + C (pacer) first. Those two alone unlock a dramatically faster voice-agent path in pure Python FastAPI code:
- WAV wrap: 1000 μs → 5 μs (reclaims 5% of per-frame budget)
- Pacer: replaces the chokepoint asyncio.sleep with precise tokio timing

D (mixer) and B (resample) are solid follow-ups. E (VAD) has limited upside without model quantization.

---

### What Pipecat changes would benefit

If Pipecat adopted these upstream (optional — users can subclass without PRs):

- Drop WAV-header recompute in `FastAPIWebsocketOutputTransport.write_audio_frame()` → `WavStreamer.wrap()` shim: **5-10 μs saved/frame**
- Replace `SOXRStreamAudioResampler` with our Rust resampler: **300-600 μs saved/frame**
- Swap `_write_audio_sleep`'s `asyncio.sleep` for our tokio pacer: **precision win, no raw speed**
- Replace `SoundfileMixer._mix_with_sound` with our SIMD mixer: **50-90 μs saved/frame**

**Cumulative per-frame savings with Phase 2 installed: ~700-1200 μs** when all features are used.

At 50 fps × 200 concurrent streams: that's ~10 cores of saved CPU.

---

## Phase 3 — rejected designs (documented so we don't re-litigate)

- `ws.run_echo()` / `ws.run_forward()` / `ws.on("message", handler)` — non-standard extensions that break the FastAPI mental model. Users should write the standard `while True: await ws.receive_bytes()` loop; we make it fast.
- Fixed-size message batching (`receive_batch(n=5)`) — adds 100 ms latency per 5 frames, destroys conversational AI. Already rejected.
- Full SIP/RTP stack — that's `agent-transport`. Out of scope.
