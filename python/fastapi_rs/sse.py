"""Server-Sent Events support matching FastAPI 0.136+."""
from __future__ import annotations
import json
from typing import Any, AsyncGenerator, Generator

KEEPALIVE_COMMENT = ": keepalive"


class ServerSentEvent:
    """Structured SSE event."""
    def __init__(self, *, data=None, event=None, id=None, retry=None, comment=None, raw_data=None):
        self.data = data
        self.event = event
        self.id = id
        self.retry = retry
        self.comment = comment
        self.raw_data = raw_data


def format_sse_event(event: ServerSentEvent | dict | str) -> str:
    """Format an SSE event to wire format."""
    if isinstance(event, str):
        return f"data: {event}\n\n"
    if isinstance(event, dict):
        event = ServerSentEvent(**event)
    lines = []
    if event.comment:
        lines.append(f": {event.comment}")
    if event.event:
        lines.append(f"event: {event.event}")
    if event.id is not None:
        lines.append(f"id: {event.id}")
    if event.retry is not None:
        lines.append(f"retry: {event.retry}")
    data = event.raw_data or event.data
    if data is not None:
        if not isinstance(data, str):
            data = json.dumps(data)
        for line in data.split("\n"):
            lines.append(f"data: {line}")
    lines.append("")
    lines.append("")
    return "\n".join(lines)
