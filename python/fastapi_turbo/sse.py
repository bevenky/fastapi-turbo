"""Server-Sent Events support matching FastAPI 0.136+.

Mirrors ``fastapi.sse`` — the module FA introduced for the ``yield``
pattern inside an ``EventSourceResponse`` path operation.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class ServerSentEvent(BaseModel):
    """Represents a single Server-Sent Event.

    When ``yield``ed from a path operation function that uses
    ``response_class=EventSourceResponse``, each ``ServerSentEvent`` is
    encoded into SSE wire format (``text/event-stream``).

    If you yield a plain object (dict, Pydantic model, etc.) instead,
    it's automatically JSON-encoded and sent as the ``data:`` field.
    """

    data: Any = None
    raw_data: str | None = None
    event: str | None = None
    id: str | None = None
    retry: int | None = Field(default=None, ge=0)
    comment: str | None = None

    @model_validator(mode="after")
    def _check_id_no_null(self) -> "ServerSentEvent":
        if self.id is not None and "\0" in self.id:
            raise ValueError("SSE 'id' must not contain null characters")
        if self.data is not None and self.raw_data is not None:
            raise ValueError(
                "Cannot set both 'data' and 'raw_data' on the same "
                "ServerSentEvent. Use 'data' for JSON-serialized payloads "
                "or 'raw_data' for pre-formatted strings."
            )
        return self


def format_sse_event(
    *,
    data_str: str | None = None,
    event: str | None = None,
    id: str | None = None,
    retry: int | None = None,
    comment: str | None = None,
) -> bytes:
    """Build SSE wire-format bytes from pre-serialized data.

    Always ends with ``\\n\\n`` (the event terminator).
    """
    lines: list[str] = []
    if comment is not None:
        for line in comment.splitlines():
            lines.append(f": {line}")
    if event is not None:
        lines.append(f"event: {event}")
    if data_str is not None:
        for line in data_str.splitlines():
            lines.append(f"data: {line}")
    if id is not None:
        lines.append(f"id: {id}")
    if retry is not None:
        lines.append(f"retry: {retry}")
    lines.append("")
    lines.append("")
    return "\n".join(lines).encode("utf-8")


# Keep-alive comment, per the SSE spec recommendation.
KEEPALIVE_COMMENT = b": ping\n\n"

# Seconds between keep-alive pings when a generator is idle.
# Private but importable so tests can monkeypatch it.
_PING_INTERVAL: float = 15.0


def __getattr__(name: str):
    # Lazy re-export so `from fastapi_turbo.sse import EventSourceResponse`
    # mirrors `fastapi.sse.EventSourceResponse`. The class lives in
    # fastapi_turbo.responses; importing it at module load would create a
    # circular import (responses.py re-uses ServerSentEvent from here).
    if name == "EventSourceResponse":
        from fastapi_turbo.responses import EventSourceResponse as _ESR
        return _ESR
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ServerSentEvent",
    "EventSourceResponse",
    "format_sse_event",
    "KEEPALIVE_COMMENT",
]
