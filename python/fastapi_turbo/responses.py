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
]


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
