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
]


async def _drive_stream(aiter, push):
    """Single-driver for an async streaming body — the Rust door's hot path.

    The naive Rust loop drove EACH ``__anext__`` through its own
    ``run_until_complete`` on a thread-local event loop: one full asyncio
    loop iteration per chunk (~37µs/chunk). This coroutine consumes the
    WHOLE async iterator under a single ``run_until_complete``, amortizing
    the loop machinery across every chunk (N run_until_complete → 1).

    ``aiter`` is the StreamingResponse's ``body_iterator`` — which, for
    request-scope yield-dep streams, is already the teardown-WRAPPED
    async-gen (``_door_wrap_stream_teardown``), so ``async for`` over it
    runs that wrapper's ``finally: _teardown()`` automatically. (Note:
    Starlette threadpool-wraps SYNC stream content into an async-gen via
    ``iterate_in_threadpool``, so this async driver handles sync content too.)

    ``push`` is the Rust ``ChunkPush`` callback: ``push(item) -> bool``;
    it converts the item to bytes and blocking-sends it through the mpsc
    channel, returning ``False`` when the receiver was dropped (client
    disconnect / door closed the body). On ``False`` we ``break`` and call
    ``aclose()`` — which throws ``GeneratorExit`` into the gen so its
    ``try/finally`` + ``except GeneratorExit`` fire (streaming-cancellation
    parity). ``aclose()`` runs ONLY on the break (disconnect) path: when the
    ``async for`` exhausts normally OR raises mid-stream, the gen is already
    finished and its ``finally`` (teardown) has run — calling ``aclose()`` on
    a threadpool-wrapped gen in that state can hang under the thread-local
    loop, and is redundant. A mid-stream raise propagates out of ``async for``
    → out of this coroutine → out of ``run_until_complete`` as a Rust
    ``Err``, where the door captures it onto
    ``app._captured_server_exceptions`` (TestClient parity).
    """
    disconnected = False
    async for item in aiter:
        if not push(item):
            disconnected = True
            break
    if disconnected:
        aclose = getattr(aiter, "aclose", None)
        if aclose is not None:
            await aclose()


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
