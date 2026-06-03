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
    """``json.dumps`` ``default=`` callback for the Rust orjson hot path —
    matches ``starlette.responses.JSONResponse`` semantics (NOT
    ``jsonable_encoder``): the dict-response fast path in ``responses.rs``
    passes this so Decimals raise (encoder-driven paths convert them first),
    bytes become UTF-8, BaseModel → ``model_dump(by_alias=True)``, Enum →
    ``.value``, else ``str(obj)``."""
    import decimal as _decimal

    if isinstance(obj, _decimal.Decimal):
        raise TypeError("Object of type Decimal is not JSON serializable")
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj).decode("utf-8", errors="replace")
    _md = getattr(obj, "model_dump", None)
    if callable(_md):
        try:
            return _md(by_alias=True)
        except Exception:  # noqa: BLE001
            pass
    import enum as _enum

    if isinstance(obj, _enum.Enum):
        return obj.value
    return str(obj)
