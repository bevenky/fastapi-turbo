"""Server-Sent Events — re-export of the REAL ``fastapi.sse``.

Imported during ``fastapi_turbo`` package init, BEFORE the compat shim
touches ``sys.modules``, so ``fastapi.sse`` here is always the genuine
upstream module. Keeping a single class identity matters: the engine
isinstance-checks ``ServerSentEvent`` on yielded items (applications.py
SSE wrap), and user code may import the class from either
``fastapi.sse`` or ``fastapi_turbo.sse`` — both must be the SAME class
or real-module SSE payloads serialize as raw model dumps instead of
``event:``/``data:`` wire lines.
"""
from __future__ import annotations

from fastapi.sse import (  # noqa: F401
    KEEPALIVE_COMMENT,
    EventSourceResponse,
    ServerSentEvent,
    _PING_INTERVAL,
    format_sse_event,
)

__all__ = [
    "ServerSentEvent",
    "EventSourceResponse",
    "format_sse_event",
    "KEEPALIVE_COMMENT",
]
