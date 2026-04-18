"""Response classes matching FastAPI/Starlette's interface."""

from __future__ import annotations

import email.utils
import os
from typing import Any


class _MutableHeadersDict(dict):
    """A dict subclass with Starlette MutableHeaders-compatible methods.

    Keeps full C-level dict compatibility (for Rust PyO3 access) while
    adding append() and getlist() that Starlette's MutableHeaders provides.
    """

    def append(self, key: str, value: str) -> None:
        """Add a header value. Overwrites if key exists (simplified single-value)."""
        self[key.lower()] = str(value)

    def getlist(self, key: str) -> list[str]:
        """Return all values for a header key as a list."""
        val = self.get(key.lower())
        if val is None:
            return []
        return [val]


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
        self.headers = _MutableHeadersDict(headers or {})
        # raw_headers preserves duplicate keys (needed for multiple Set-Cookie).
        # Rust side reads this list with header.append() instead of insert().
        self.raw_headers: list[tuple[str, str]] = []
        self.background = background

        if media_type is not None:
            self.media_type = media_type

        if self.media_type:
            self.headers.setdefault("content-type", self.media_type)

        self.body = self.render(content)

    def init_headers(self, headers=None):
        """Starlette compatibility — called by subclasses like sse_starlette's
        EventSourceResponse during __init__."""
        if not hasattr(self, "headers"):
            self.headers = {}
        if not hasattr(self, "raw_headers"):
            self.raw_headers = []
        if headers:
            if isinstance(headers, dict):
                self.headers.update(headers)
            elif hasattr(headers, "items"):
                self.headers.update(dict(headers.items()))
            else:
                for item in headers:
                    if isinstance(item, (list, tuple)) and len(item) == 2:
                        k, v = item
                        self.headers[k if isinstance(k, str) else k.decode()] = (
                            v if isinstance(v, str) else v.decode()
                        )
        if self.media_type and "content-type" not in {k.lower() for k in self.headers}:
            self.headers["content-type"] = self.media_type

    def render(self, content) -> bytes:
        if content is None:
            return b""
        if isinstance(content, bytes):
            return content
        return content.encode("utf-8")

    def set_cookie(
        self,
        key: str,
        value: str = "",
        max_age: int | None = None,
        expires: Any = None,
        path: str | None = "/",
        domain: str | None = None,
        secure: bool = False,
        httponly: bool = False,
        samesite: str | None = "lax",
        partitioned: bool = False,
    ) -> None:
        """Set a Set-Cookie header on this response (Starlette-compatible signature).

        All parameters are positional-or-keyword to match Starlette exactly.
        """
        parts: list[str] = [f"{key}={value}"]
        if max_age is not None:
            parts.append(f"Max-Age={int(max_age)}")
        if expires is not None:
            if isinstance(expires, (int, float)):
                parts.append(f"Expires={email.utils.formatdate(float(expires), usegmt=True)}")
            else:
                parts.append(f"Expires={expires}")
        if path is not None:
            parts.append(f"Path={path}")
        if domain is not None:
            parts.append(f"Domain={domain}")
        if secure:
            parts.append("Secure")
        if httponly:
            parts.append("HttpOnly")
        if samesite is not None:
            parts.append(f"SameSite={samesite.capitalize()}")
        if partitioned:
            parts.append("Partitioned")
        cookie_str = "; ".join(parts)
        self.raw_headers.append(("set-cookie", cookie_str))

    def delete_cookie(
        self,
        key: str,
        path: str = "/",
        domain: str | None = None,
        secure: bool = False,
        httponly: bool = False,
        samesite: str | None = "lax",
    ) -> None:
        """Clear a cookie by setting max_age=0 (Starlette-compatible signature)."""
        self.set_cookie(
            key,
            "",
            0,      # max_age
            0,      # expires
            path,
            domain,
            secure,
            httponly,
            samesite,
        )


class JSONResponse(Response):
    """JSON response. Uses orjson when available, else stdlib json.

    orjson is roughly 5× faster than stdlib json but is an optional dependency.
    """

    media_type = "application/json"

    def render(self, content) -> bytes:
        try:
            import orjson
            return orjson.dumps(content, default=float)
        except ImportError:
            import json
            return json.dumps(content, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


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
        self.headers = _MutableHeadersDict(headers or {})
        self.raw_headers: list[tuple[str, str]] = []
        self.media_type = media_type
        self.background = background
        self.body = b""  # placeholder — Rust handles streaming

        if self.media_type:
            self.headers.setdefault("content-type", self.media_type)

    async def listen_for_disconnect(self):
        """Wait until the client disconnects.

        Stub -- client disconnect detection is handled by the Rust layer.
        Matches Starlette behavior: blocks forever (never returns).
        """
        import asyncio
        await asyncio.sleep(float('inf'))


class ORJSONResponse(Response):
    """JSON response using orjson for serialization (with stdlib json fallback)."""

    media_type = "application/json"

    def render(self, content) -> bytes:
        try:
            import orjson
            return orjson.dumps(content, default=float)
        except ImportError:
            import json
            return json.dumps(content, ensure_ascii=False, default=str).encode("utf-8")


class UJSONResponse(Response):
    """JSON response using ujson for serialization (with stdlib json fallback)."""

    media_type = "application/json"

    def render(self, content) -> bytes:
        try:
            import ujson
            return ujson.dumps(content, ensure_ascii=False).encode("utf-8")
        except ImportError:
            import json
            return json.dumps(content, ensure_ascii=False).encode("utf-8")


class FileResponse(Response):
    """Serve a file from disk.

    The Rust layer streams the file with proper Content-Type detection,
    Content-Length, ETag, Last-Modified, and Range request handling
    (HTTP 206 Partial Content for `Range: bytes=N-M`).

    Usage::

        @app.get("/download/{name}")
        def download(name: str):
            return FileResponse(f"/var/uploads/{name}")

        # With custom filename (becomes Content-Disposition):
        return FileResponse(path, filename="report.pdf")

        # Force download (Content-Disposition: attachment):
        return FileResponse(path, filename="doc.pdf")
    """

    media_type = None  # inferred from file extension if not given

    def __init__(
        self,
        path,
        status_code: int = 200,
        headers=None,
        media_type: str | None = None,
        filename: str | None = None,
        content_disposition_type: str = "attachment",
        method: str | None = None,
        stat_result: Any = None,
        background=None,
    ):
        import mimetypes

        self.path = str(path)
        self.filename = filename
        self.content_disposition_type = content_disposition_type
        self.stat_result = stat_result
        # Infer media type from extension if not explicitly given
        if media_type is None:
            guessed, _ = mimetypes.guess_type(self.path)
            media_type = guessed or "application/octet-stream"

        super().__init__(
            content=b"",
            status_code=status_code,
            headers=headers,
            media_type=media_type,
            background=background,
        )
        # Stamp Content-Disposition if filename provided
        if self.filename:
            self.headers["content-disposition"] = (
                f'{self.content_disposition_type}; filename="{self.filename}"'
            )
        # Apply stat headers if stat_result was provided
        if self.stat_result is not None:
            self.set_stat_headers(self.stat_result)

    def set_stat_headers(self, stat_result) -> None:
        """Set content-length and last-modified headers from an os.stat_result."""
        self.headers["content-length"] = str(stat_result.st_size)
        self.headers["last-modified"] = email.utils.formatdate(
            stat_result.st_mtime, usegmt=True
        )
