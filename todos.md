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

## Phase 2 (future — NOT in this scope doc)

Separate audio helper modules (Rust-backed, Python API), released independently:
- `fastapi_rs.audio.codec` — Opus, G.711 μ-law / A-law encode/decode
- `fastapi_rs.audio.resample` — 8k↔16k↔24k↔48k via speexdsp
- `fastapi_rs.audio.vad` — Silero or WebRTC VAD
- `fastapi_rs.audio.mixer` — PCM mix with clipping
- `fastapi_rs.audio.jitter` — network jitter buffer

None of these change the WebSocket API. They're tools users can import alongside our WS to build agent-transport-class apps in pure Python.

---

## Phase 3 (future — NOT in this scope doc)

Explicitly NOT planned. Listed to document the rejected options:
- `ws.run_echo()` / `ws.run_forward()` / `ws.on("message", handler)` — non-standard, breaks FastAPI mental model.
- Fixed-size message batching — incompatible with real-time audio latency.
- Full SIP/RTP stack — out of scope; that's what agent-transport is for.
