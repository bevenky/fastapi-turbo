"""Response classes matching FastAPI/Starlette's interface."""

from __future__ import annotations

import email.utils
import os
from typing import Any


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
        # raw_headers preserves duplicate keys (needed for multiple Set-Cookie).
        # Rust side reads this list with header.append() instead of insert().
        self.raw_headers: list[tuple[str, str]] = []
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
            return orjson.dumps(content)
        except ImportError:
            import json
            return json.dumps(content, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


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
        self.raw_headers: list[tuple[str, str]] = []
        self.media_type = media_type
        self.background = background
        self.body = b""  # placeholder — Rust handles streaming

        if self.media_type:
            self.headers.setdefault("content-type", self.media_type)


class ORJSONResponse(Response):
    """JSON response using orjson for serialization (with stdlib json fallback)."""

    media_type = "application/json"

    def render(self, content) -> bytes:
        try:
            import orjson
            return orjson.dumps(content)
        except ImportError:
            import json
            return json.dumps(content, ensure_ascii=False).encode("utf-8")


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
