"""Response classes matching FastAPI/Starlette's interface."""

from __future__ import annotations

import os


class Response:
    """Base HTTP response."""

    media_type: str | None = None

    def __init__(
        self,
        content=None,
        status_code: int = 200,
        headers=None,
        media_type: str | None = None,
        background=None,
    ):
        self.status_code = status_code
        self.headers: dict[str, str] = dict(headers or {})
        self.background = background

        if media_type is not None:
            self.media_type = media_type

        if self.media_type:
            self.headers.setdefault("content-type", self.media_type)

        self.body = self.render(content)

    def render(self, content) -> bytes:
        if content is None:
            return b""
        if isinstance(content, bytes):
            return content
        return content.encode("utf-8")


class JSONResponse(Response):
    """JSON response using orjson for serialization."""

    media_type = "application/json"

    def render(self, content) -> bytes:
        import orjson

        return orjson.dumps(content)


class HTMLResponse(Response):
    """HTML response."""

    media_type = "text/html"


class PlainTextResponse(Response):
    """Plain text response."""

    media_type = "text/plain"


class RedirectResponse(Response):
    """HTTP redirect response."""

    def __init__(self, url, status_code: int = 307, headers=None, background=None):
        super().__init__(content=b"", status_code=status_code, headers=headers, background=background)
        self.headers["location"] = str(url)


class StreamingResponse(Response):
    """Streaming response — the body iterator is consumed by the Rust server."""

    def __init__(
        self,
        content,
        status_code: int = 200,
        headers=None,
        media_type: str | None = None,
        background=None,
    ):
        self.body_iterator = content
        self.status_code = status_code
        self.headers: dict[str, str] = dict(headers or {})
        self.media_type = media_type
        self.background = background
        self.body = b""  # placeholder — Rust handles streaming

        if self.media_type:
            self.headers.setdefault("content-type", self.media_type)


class FileResponse(Response):
    """Serve a file from disk."""

    def __init__(
        self,
        path,
        status_code: int = 200,
        headers=None,
        media_type: str | None = None,
        filename: str | None = None,
        method: str | None = None,
        background=None,
    ):
        self.path = path
        self.filename = filename or os.path.basename(path)
        super().__init__(
            content=b"",
            status_code=status_code,
            headers=headers,
            media_type=media_type,
            background=background,
        )
