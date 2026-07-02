"""Response classes — re-exported from REAL starlette / fastapi.

The Python clone reimplementation (Response + _MutableHeadersDict/_LiveRawHeaders
+ all subclasses) was retired in favor of the real packages. The Rust door was
already prepared to read real Starlette's response structure:
  * ``extract_header_pair`` (responses.rs) accepts ``raw_headers`` as
    ``(bytes, bytes)`` (real) as well as ``(str, str)`` (the old clone);
  * ``response_object_to_response`` reads ``.headers`` via either a plain dict
    or ``MutableHeaders.items()``, and skips keys already covered by
    ``raw_headers`` so real Starlette's raw-headers-backed ``MutableHeaders``
    de-duplicates correctly.

Imported during ``fastapi_turbo`` package init, BEFORE the compat shim rebinds
``sys.modules``, so ``starlette.responses`` / ``fastapi.responses`` / ``fastapi.sse``
resolve to the REAL packages; the bound references stay real after the shim runs.
``_json_default`` is kept here because the Rust JSON hot path imports it
(``responses.rs`` ``json_default``).
"""
from __future__ import annotations

import asyncio as _asyncio
import types as _types

from starlette.responses import (
    Response,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
    FileResponse,
)
from fastapi.responses import JSONResponse, ORJSONResponse, UJSONResponse
from fastapi.sse import EventSourceResponse

__all__ = [
    "Response",
    "JSONResponse",
    "HTMLResponse",
    "PlainTextResponse",
    "RedirectResponse",
    "StreamingResponse",
    "FileResponse",
    "ORJSONResponse",
    "UJSONResponse",
    "EventSourceResponse",
    "_json_default",
    "_drive_stream",
    "_resolve_stream_future",
    "_resume_anext",
    "_spawn_stream_task",
    "_stream_is_noawait",
]


_STREAM_DONE = object()


@_types.coroutine
def _step_anext(coro):
    """Drive ONE ``__anext__()`` coroutine, fast-pathing cooperative yields.

    ``await asyncio.sleep(0)`` (and anyio's checkpoints) are *bare* cooperative
    yields — they yield ``None`` and register nothing on the loop. Measured on
    uvloop, a real ``await sleep(0)`` round-trips the loop at ~13µs/chunk, while
    resuming the coroutine inline with ``send(None)`` costs ~0.1µs — a 40-100×
    gap, purely for a loop trip the chunk never needed.

    So we step the coroutine by hand: on a ``None`` yield (cooperative
    checkpoint) we resume INLINE with another ``send(None)`` — no loop
    round-trip. On a yield of a *real* awaitable (a Future, from genuine I/O —
    ``asyncio.sleep(>0)``, a socket read, a DB driver), we ``yield`` it up to
    the running Task so the event loop drives the I/O and resumes us when it
    completes (preserving correctness AND cross-stream overlap). Returns the
    ``StopIteration`` value (the chunk) on completion, or ``_STREAM_DONE`` on
    ``StopAsyncIteration``. Other exceptions (mid-stream raises) propagate.

    Safe by construction: this runs UNDER ``run_until_complete`` (a running
    loop), so a real ``await`` that calls ``get_running_loop()`` succeeds and
    yields a Future — never the bare-send ``RuntimeError`` that corrupts a gen
    when stepped with no loop. Verified equivalent to ``async for`` (real waits
    honored to the ms; concurrent streams overlap their waits).
    """
    to_send = None
    to_throw = None
    while True:
        try:
            if to_throw is not None:
                x = coro.throw(to_throw)
            else:
                x = coro.send(to_send)
        except StopIteration as e:
            return e.value
        except StopAsyncIteration:
            return _STREAM_DONE
        to_send = None
        to_throw = None
        if x is None:
            continue          # cooperative checkpoint — resume inline, no loop trip
        # Real awaitable: forward to the running Task (loop drives the I/O).
        # Whatever the loop sends back on resume — a value OR an exception
        # (e.g. CancelledError from wait_for, a future's exception) — must be
        # propagated INTO the inner coroutine so its own try/except handles it
        # (parity with ``await``). Forwarding only ``send`` would let those
        # exceptions escape and skip the gen's handlers.
        try:
            to_send = yield x
        except BaseException as e:  # noqa: BLE001 — re-injected into coro below
            to_throw = e


async def _drive_stream(aiter, push):
    """Single-driver for an async streaming body — the Rust door's hot path.

    ONE ``run_until_complete`` consumes the WHOLE async iterator (vs the naive
    per-chunk loop), and within it ``_step_anext`` short-circuits cooperative
    ``await sleep(0)`` checkpoints inline (~0.1µs) instead of paying a full
    asyncio loop iteration (~13µs) per chunk — real I/O awaits still defer to
    the loop and overlap across streams.

    ``aiter`` is the StreamingResponse's ``body_iterator`` — which, for
    request-scope yield-dep streams, is already the teardown-WRAPPED
    async-gen (``_door_wrap_stream_teardown``), so stepping its ``__anext__``
    runs that wrapper's ``finally: _teardown()`` automatically. (Starlette
    threadpool-wraps SYNC stream content into an async-gen via
    ``iterate_in_threadpool``, so this async driver handles sync content too.)

    ``push`` is a Rust per-chunk callback with a tri-state result:

    * ``True``  — chunk sent, keep going (the common case).
    * ``False`` — the receiver was dropped (client disconnect / door closed
      the body). We ``break`` and ``aclose()`` — throwing ``GeneratorExit``
      into the gen so its ``try/finally`` fires (cancellation parity).
    * an *awaitable* (worker-loop driver only, ``LoopChunkPush``) — the body
      channel is FULL (slow-consumer backpressure). The legacy dedicated-
      thread driver (``ChunkPush``) blocks the thread instead, but a task on
      the SHARED worker loop must never block it — Rust hands back an asyncio
      Future that resolves ``True`` once capacity freed (the pending chunk is
      sent by the Rust waiter itself, order preserved) or ``False`` when the
      receiver went away while waiting.

    ``aclose()`` runs ONLY on the disconnect path: on normal exhaustion /
    mid-stream raise the gen is already finished. A mid-stream raise
    propagates out (task exception on the worker loop / Rust ``Err`` on the
    legacy path), captured onto ``app._captured_server_exceptions``
    (TestClient parity).

    Fairness: ``_step_anext`` resumes cooperative checkpoints INLINE, so a
    gen that only ever ``await sleep(0)``s between chunks would otherwise run
    the WHOLE stream without yielding to the loop once — starving every other
    task when this driver multiplexes on the shared worker loop. A real
    ``sleep(0)`` every 64 chunks caps that burst (~0.2 µs/chunk amortized;
    noise for gens that do real I/O).
    """
    it = aiter.__aiter__()
    disconnected = False
    n = 0
    while True:
        chunk = await _step_anext(it.__anext__())
        if chunk is _STREAM_DONE:
            break
        sent = push(chunk)
        if sent is not True:
            if sent is False or not await sent:
                disconnected = True
                break
        n += 1
        if not (n & 63):
            await _asyncio.sleep(0)
    if disconnected:
        aclose = getattr(aiter, "aclose", None)
        if aclose is not None:
            await aclose()
    # Normal completion: drop the channel Sender NOW so the HTTP body's EOF
    # doesn't wait for the task done-callback (one loop-callback hop earlier).
    # On a mid-stream raise we deliberately do NOT reach this line — the
    # StreamCompleter captures the exception onto the app FIRST and closes
    # after (TestClient must see the error once the body ends). The legacy
    # ChunkPush also has close(); there it's a harmless early drop (the
    # driving closure holds its own Sender clone until after capture).
    push.close()


async def _resume_anext(started):
    """Resume an already-started ``__anext__()`` coroutine from its suspension.

    The Rust no-await fast path (``streaming.rs::iterate_async_generator``)
    drives each ``aiter.__anext__()`` with a single bare ``coro.send(None)``
    on the streaming thread WITHOUT an event loop. When that step reaches
    ``StopIteration(chunk)`` the chunk is pushed inline (no loop). When the
    step instead SUSPENDS (the gen really awaited something — ``asyncio.sleep``,
    a memory-stream ``receive``, SSE's ``wait_for``), ``send(None)`` returns a
    value and the Rust side hands the *already-started* coro here, running this
    coroutine on the shared ``STREAM_LOOP`` via ``run_until_complete``.

    ``await started`` correctly resumes the coro from exactly where it
    suspended — it does NOT re-send ``None`` from the front (which would trip
    "async generator already running"). It returns the gen's next item (or
    propagates ``StopAsyncIteration`` / a mid-stream raise), so the whole
    stream's loop-needing chunks all run on the SAME loop, keeping SSE's
    ``ensure_future`` producer task alive across chunks.
    """
    return await started


def _spawn_stream_task(loop, coro, completer):
    """Spawn the ``_drive_stream`` driver as a task on the worker loop.

    Called from ``streaming.rs::StreamJob.__call__`` ON the loop thread (the
    loop is running). ``eager_start=True`` (3.12+) runs the driver's first
    step synchronously right here — a cooperative-only stream (checkpoints
    fast-pathed inline by ``_step_anext``) completes its WHOLE body without a
    single extra loop iteration, and ``push.close()`` at the driver's end has
    already closed the body channel before this returns. Unlike a bare
    ``coro.send(None)`` probe, an eager Task is a REAL current task —
    ``asyncio.current_task()``-dependent code (``wait_for``/``timeout`` in the
    SSE keepalive wrap) sees a proper task context.

    The completer (exception capture + guaranteed channel close) is attached
    AFTER an eager completion, which is fine: ``add_done_callback`` on a done
    task schedules it via ``call_soon``, and the mid-stream-raise path leaves
    the channel open until the completer captures-then-closes (ordering
    parity with the legacy driver).
    """
    try:
        task = _asyncio.Task(coro, loop=loop, eager_start=True)
    except TypeError:  # Python < 3.12 — no eager_start
        task = loop.create_task(coro)
    task.add_done_callback(completer)


def _resolve_stream_future(fut, ok):
    """``call_soon_threadsafe`` target: resolve a stream-backpressure future.

    The worker-loop stream driver (``streaming.rs::LoopChunkPush``) hands
    ``_drive_stream`` an asyncio Future when the body channel is full; a Rust
    waiter task awaits channel capacity, sends the pending chunk itself, then
    schedules this resolver onto the worker loop. Guard on ``done()`` so a
    future that was cancelled in the meantime (loop shutdown) doesn't raise
    ``InvalidStateError`` into the loop's exception handler."""
    if not fut.done():
        fut.set_result(ok)


_NOAWAIT_STREAM_CACHE: dict[int, bool] = {}


def _gen_is_noawait(gen) -> bool:
    """Bytecode check: does this async generator's OWN body never ``await``?

    An ``await`` expression compiles to the ``GET_AWAITABLE`` opcode; its
    absence in the gen's code object proves the body never awaits, so every
    ``__anext__`` reaches ``StopIteration`` on a bare ``send(None)`` (verified)
    — never the destructive ``RuntimeError('no running event loop')`` that a
    loop-needing ``await`` raises on a bare send (which corrupts the gen: the
    partly-advanced ``__anext__`` can't be resumed and the gen then reports
    ``StopAsyncIteration``, silently truncating the stream).

    NOTE: this looks ONLY at the gen's own code, not at anything it iterates.
    Wrapper gens (e.g. ``async for x in inner: yield x``) have no
    ``GET_AWAITABLE`` of their own yet are await-ing iff ``inner`` awaits, so a
    wrapper must NOT be analyzed directly — its verdict is stamped from the
    wrapped ``inner`` (see ``_door_wrap_stream_teardown``). Cached by
    code-object id."""
    import dis

    code = getattr(gen, "ag_code", None)
    if code is None:
        return False
    key = id(code)
    cached = _NOAWAIT_STREAM_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        has_await = any(
            ins.opname == "GET_AWAITABLE" for ins in dis.get_instructions(code)
        )
        verdict = not has_await
    except Exception:  # noqa: BLE001
        verdict = False
    _NOAWAIT_STREAM_CACHE[key] = verdict
    return verdict


def _stream_is_noawait(response) -> bool:
    """Decide whether the Rust streaming door may drive a response's async
    ``body_iterator`` inline with bare ``send(None)`` (no event loop) instead
    of the per-chunk ``run_until_complete`` path. Returns True ONLY for async
    generators proven not to await a loop-needing primitive — see
    ``_gen_is_noawait``.

    ``streaming.rs`` now reads the stamped flag / caches ``_gen_is_noawait``
    verdicts per code object itself (``stream_noawait_verdict``); this helper
    remains the reference implementation of the decision. Order:
      1. An explicit ``_fastapi_turbo_stream_noawait`` flag (a bool) on the
         response wins — set by ``_door_wrap_stream_teardown`` from the WRAPPED
         user gen, so a teardown wrapper inherits the real gen's verdict
         instead of being (mis)analyzed as no-await just because its own body
         only does ``async for`` (a wrapper has no ``GET_AWAITABLE`` of its
         own yet awaits iff ``inner`` does).
      2. Otherwise analyze the body_iterator's own bytecode (the raw
         ``StreamingResponse(user_gen())`` case, where ``body_iterator`` IS the
         user gen)."""
    flag = getattr(response, "_fastapi_turbo_stream_noawait", None)
    if flag is not None:
        return bool(flag)
    return _gen_is_noawait(getattr(response, "body_iterator", None))


def _json_default(obj):
    """``orjson.dumps`` ``default=`` callback for the Rust JSON hot path
    (``responses.rs::dict_to_json_bytes``).

    Mirrors the Rust ``write_any_json`` writer (which serves as the full
    fallback when orjson raises) so both paths emit identical bytes:
    Decimals raise (the raise fails the whole orjson call, dropping the
    payload onto the Rust writer, which emits them as JSON numbers),
    bytes → UTF-8, set/frozenset → list, timedelta → ``total_seconds()``
    (int when whole), BaseModel → ``model_dump(mode="json",
    by_alias=True)``, Enum → ``.value``, objects → ``vars(obj)``, else
    ``str(obj)``. orjson natively handles datetime/date/time, UUID,
    dataclasses, and tuples, so those never reach this callback."""
    import decimal as _decimal

    if isinstance(obj, _decimal.Decimal):
        raise TypeError("Object of type Decimal is not JSON serializable")
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj).decode("utf-8", errors="replace")
    if isinstance(obj, (set, frozenset)):
        return list(obj)
    import datetime as _dt

    if isinstance(obj, _dt.timedelta):
        secs = obj.total_seconds()
        # FA emits integers when whole; mimic (matches write_any_json).
        return int(secs) if secs == int(secs) else secs
    _md = getattr(obj, "model_dump", None)
    if callable(_md):
        try:
            return _md(mode="json", by_alias=True)
        except Exception:  # noqa: BLE001
            pass
    import enum as _enum

    if isinstance(obj, _enum.Enum):
        return obj.value
    d = getattr(obj, "__dict__", None)
    if isinstance(d, dict):
        return d
    return str(obj)
