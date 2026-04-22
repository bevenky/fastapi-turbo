"""Response classes matching FastAPI/Starlette's interface."""

from __future__ import annotations

import email.utils
import os
from typing import Any


class _MutableHeadersDict(dict):
    """A dict subclass with Starlette MutableHeaders-compatible methods.

    Keeps full C-level dict compatibility (for Rust PyO3 access) while
    adding append() / getlist() / mutablecopy() etc. that Starlette's
    MutableHeaders provides. Duplicate values for the same header name
    (e.g. two `Vary:` headers, multiple `Set-Cookie:`) are preserved in
    `_extras` and reflected in `raw`, `items()`, iteration, and Rust-side
    emission via the owning Response's `raw_headers`.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Each entry is a (lowercased-key, raw-value) tuple. Mirror to the
        # owning Response's `raw_headers` via `_link_response()` so the Rust
        # renderer emits multi-value headers verbatim.
        self._extras: list[tuple[str, str]] = []
        self._linked_raw: list[tuple[str, str]] | None = None

    def _link_raw_headers(self, raw: list[tuple[str, str]]) -> None:
        self._linked_raw = raw

    def append(self, key: str, value: str) -> None:
        """Add a header value, preserving duplicates (Starlette-compatible).

        Starlette's `MutableHeaders.append()` pushes a new (key, value) onto
        the raw headers list — so two `append("X-Dup", "a")`+`append("X-Dup",
        "b")` calls produce TWO `X-Dup` headers. The dict view still
        reflects the latest value (so `.get(key)` stays predictable).
        """
        key_l = key.lower()
        val = str(value)
        self[key_l] = val
        self._extras.append((key_l, val))
        if self._linked_raw is not None:
            self._linked_raw.append((key_l, val))

    def getlist(self, key: str) -> list[str]:
        """Return all values for a header key as a list."""
        key_l = key.lower()
        vals: list[str] = []
        # Capture the single-valued canonical plus any extras that share the
        # name. Iteration order matches insertion order.
        if key_l in self and not any(k == key_l for k, _ in self._extras):
            vals.append(self[key_l])
        for k, v in self._extras:
            if k == key_l:
                vals.append(v)
        return vals

    def mutablecopy(self) -> "_MutableHeadersDict":
        """Return a new MutableHeaders instance with identical entries.

        Starlette exposes this so middleware can hand a detachable copy to
        downstream code without sharing state with the live response.
        """
        copy = _MutableHeadersDict(self)
        copy._extras = list(self._extras)
        return copy

    def raw(self) -> list[tuple[bytes, bytes]]:
        """Starlette compatibility — raw (bytes, bytes) list view."""
        seen_keys: set[str] = set()
        out: list[tuple[bytes, bytes]] = []
        for k, v in self.items():
            seen_keys.add(k)
            out.append((k.encode("latin-1"), v.encode("latin-1")))
        for k, v in self._extras:
            if k in seen_keys:
                # Already emitted via dict canonical; append the extra too
                out.append((k.encode("latin-1"), v.encode("latin-1")))
        return out


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
        # raw_headers preserves duplicate keys (needed for multiple Set-Cookie
        # and `response.headers.append("X-Dup", ...)` duplicates). Rust side
        # reads this list with header.append() instead of insert().
        self.raw_headers: list[tuple[str, str]] = []
        # Link the headers object so `.append()` reflects duplicates into
        # `raw_headers` automatically.
        self.headers._link_raw_headers(self.raw_headers)
        self.background = background

        if media_type is not None:
            self.media_type = media_type

        if self.media_type:
            # Starlette auto-appends "; charset=utf-8" to text/* media
            # types. FA tests expect ``text/html; charset=utf-8`` on
            # HTMLResponse etc. — match that behaviour.
            ct = self.media_type
            if ct.startswith("text/") and "charset=" not in ct.lower():
                ct = f"{ct}; charset={getattr(self, 'charset', 'utf-8')}"
            self.headers.setdefault("content-type", ct)

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
        # Starlette's http.cookies-compatible builder emits attributes in
        # canonical alphabetic order (Domain, expires, HttpOnly, Max-Age,
        # Path, SameSite, Secure). Emitting the same order avoids
        # byte-level diffs with FA.
        import time as _time
        # Wrap values that contain spaces, commas, semicolons etc. per
        # RFC 6265 quoted-string encoding — otherwise the Set-Cookie
        # header is malformed and cookie jars drop the value.
        _encoded_value = str(value)
        if any(ch in _encoded_value for ch in ' ",;\\') and not (
            _encoded_value.startswith('"') and _encoded_value.endswith('"')
        ):
            _encoded_value = '"' + _encoded_value.replace('\\', r'\\').replace('"', r'\"') + '"'
        parts: list[str] = [f"{key}={_encoded_value}"]
        if domain is not None:
            parts.append(f"Domain={domain}")
        if expires is not None:
            if isinstance(expires, (int, float)):
                # Newer Starlette interprets an int `expires` as
                # seconds-from-now. Add the current epoch so the emitted
                # HTTP-date points at a future instant (matches FA).
                ts = float(expires)
                if ts < 315532800:  # < 1980-01-01 → treat as offset
                    ts = _time.time() + ts
                parts.append(
                    f"expires={email.utils.formatdate(ts, usegmt=True)}"
                )
            else:
                parts.append(f"expires={expires}")
        if httponly:
            parts.append("HttpOnly")
        if max_age is not None:
            parts.append(f"Max-Age={int(max_age)}")
        if path is not None:
            parts.append(f"Path={path}")
        if samesite is not None:
            # Starlette emits the samesite value lowercased on the wire
            # (`SameSite=lax`). Match that so parsers that do a strict
            # case-sensitive equality (some CDN / proxy bindings) behave
            # identically under stock FastAPI and fastapi-rs.
            parts.append(f"SameSite={samesite.lower()}")
        if secure:
            parts.append("Secure")
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


def _json_default(obj):
    """json.dumps ``default=`` callback for types that aren't JSON-native.

    Mirrors ``fastapi.encoders.jsonable_encoder`` for the types FA
    commonly sees: ``Decimal`` → str (FA's default), ``bytes`` → UTF-8
    str, ``BaseModel`` → dict via ``model_dump``, Enum → ``.value``.
    Anything else falls back to ``str(obj)``.
    """
    import decimal as _decimal
    if isinstance(obj, _decimal.Decimal):
        return str(obj)
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


class JSONResponse(Response):
    """JSON response. Uses stdlib ``json`` (Starlette parity).

    fastapi-rs's default response path bypasses this class entirely and
    serializes via Rust; this class is only hit when users explicitly
    set ``response_class=JSONResponse`` or instantiate it manually.
    """

    media_type = "application/json"

    def render(self, content) -> bytes:
        import json
        return json.dumps(
            content,
            separators=(",", ":"),
            ensure_ascii=False,
            default=_json_default,
        ).encode("utf-8")


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
        # Preserve the class-level ``media_type`` (``PNGStreamingResponse
        # .media_type = "image/png"``) when the caller didn't pass one
        # — otherwise user subclasses lose their default content-type.
        if media_type is not None:
            self.media_type = media_type
        self.background = background
        self.body = b""  # placeholder — Rust handles streaming

        if self.media_type:
            # Same charset-appending rule as ``Response`` — FA sends
            # ``text/event-stream; charset=utf-8`` for SSE streams.
            ct = self.media_type
            if ct.startswith("text/") and "charset=" not in ct.lower():
                ct = f"{ct}; charset={getattr(self, 'charset', 'utf-8')}"
            self.headers.setdefault("content-type", ct)

    async def listen_for_disconnect(self):
        """Wait until the client disconnects.

        Stub -- client disconnect detection is handled by the Rust layer.
        Matches Starlette behavior: blocks forever (never returns).
        """
        import asyncio
        await asyncio.sleep(float('inf'))


class EventSourceResponse(StreamingResponse):
    """SSE response — wraps an async/sync generator of events.

    Matches FA 0.136+ behaviour: yielded items (``ServerSentEvent``,
    Pydantic models, dicts, strings) are encoded to the SSE wire format
    (``data: <json>\\n\\n``) before being streamed. Plain strings and
    other scalars are JSON-encoded so the wire data field is a JSON
    literal (``"hello"`` becomes ``data: "hello"`` on the wire — FA's
    explicit contract).
    """
    media_type = "text/event-stream"

    def __init__(self, content, *, status_code=200, headers=None, media_type=None, background=None, ping=None, sep=None):
        _headers = dict(headers or {})
        _headers.setdefault("Cache-Control", "no-cache")
        _headers.setdefault("Connection", "keep-alive")
        _headers.setdefault("X-Accel-Buffering", "no")
        wrapped = self._encode_stream(content)
        super().__init__(
            content=wrapped,
            status_code=status_code,
            headers=_headers,
            media_type=media_type or self.media_type,
            background=background,
        )

    @staticmethod
    def _encode_stream(content):
        """Wrap the generator/iterable so each yielded item becomes SSE
        wire-format bytes — matches FA's streaming encoder exactly.

        Also emits a ``: ping\\n\\n`` keepalive comment between events
        when the underlying generator is idle for longer than
        ``fastapi.routing._PING_INTERVAL`` seconds (FA parity — allows
        tests to monkeypatch the interval).
        """
        import inspect as _inspect

        def _ping_interval() -> float:
            # Read from fastapi.routing at REQUEST time so
            # ``monkeypatch.setattr(fastapi.routing._PING_INTERVAL, 0.05)``
            # takes effect.
            try:
                import fastapi.routing as _fr
                v = getattr(_fr, "_PING_INTERVAL", None)
                if isinstance(v, (int, float)) and v > 0:
                    return float(v)
            except Exception:  # noqa: BLE001
                pass
            try:
                from fastapi_rs.sse import _PING_INTERVAL as _pi
                return float(_pi)
            except Exception:  # noqa: BLE001
                return 15.0

        if _inspect.isasyncgen(content):
            async def _async_wrap():
                import asyncio as _asyncio
                aiter = content.__aiter__()
                # Start __anext__ as a task so a timeout does NOT
                # cancel the generator's internal state — we just poll
                # again on the next iteration. ``asyncio.wait_for``
                # would cancel the coro and terminate the generator.
                pending = _asyncio.ensure_future(aiter.__anext__())
                try:
                    while True:
                        interval = _ping_interval()
                        done, _not_done = await _asyncio.wait(
                            {pending}, timeout=interval,
                        )
                        if not done:
                            yield b": ping\n\n"
                            continue
                        try:
                            item = pending.result()
                        except StopAsyncIteration:
                            return
                        yield EventSourceResponse._encode_item(item)
                        pending = _asyncio.ensure_future(aiter.__anext__())
                finally:
                    if not pending.done():
                        pending.cancel()
                        try:
                            await pending
                        except BaseException:  # noqa: BLE001
                            pass
            return _async_wrap()
        if _inspect.isgenerator(content) or hasattr(content, "__iter__"):
            # Sync generators are consumed on a thread (the Rust streaming
            # layer drives ``__next__`` on a blocking thread). Use a
            # thread+queue bridge so we can insert keepalive pings from
            # an async wrapper.
            def _sync_wrap():
                import threading as _th
                import queue as _q
                import time as _time

                q: _q.Queue = _q.Queue(maxsize=1)
                _DONE = object()

                def _producer():
                    try:
                        for item in content:
                            q.put(item)
                    except BaseException as exc:  # noqa: BLE001
                        q.put(("__error__", exc))
                        return
                    q.put(_DONE)

                t = _th.Thread(target=_producer, daemon=True)
                t.start()
                while True:
                    interval = _ping_interval()
                    try:
                        item = q.get(timeout=interval)
                    except _q.Empty:
                        yield b": ping\n\n"
                        continue
                    if item is _DONE:
                        return
                    if (
                        isinstance(item, tuple)
                        and len(item) == 2
                        and item[0] == "__error__"
                    ):
                        raise item[1]
                    yield EventSourceResponse._encode_item(item)
            return _sync_wrap()
        return content

    @staticmethod
    def _encode_item(item) -> bytes:
        """Encode one yielded item into SSE wire-format bytes.

        Accepts ``ServerSentEvent``, Pydantic models, dicts, lists,
        strings, and bytes. Everything non-bytes goes through
        ``json.dumps`` for the ``data:`` field (so strings end up
        quoted: ``"hello"``).
        """
        import json as _json
        try:
            from fastapi_rs.sse import ServerSentEvent, format_sse_event
        except ImportError:
            ServerSentEvent = None
            format_sse_event = None
        if isinstance(item, bytes):
            return item
        if ServerSentEvent is not None and isinstance(item, ServerSentEvent):
            if item.raw_data is not None:
                data_str = item.raw_data
            elif item.data is not None:
                try:
                    data_str = _json.dumps(
                        item.data.model_dump(by_alias=True)
                        if hasattr(item.data, "model_dump")
                        else item.data
                    )
                except (TypeError, ValueError):
                    data_str = _json.dumps(str(item.data))
            else:
                data_str = None
            return format_sse_event(
                data_str=data_str,
                event=item.event,
                id=item.id,
                retry=item.retry,
                comment=item.comment,
            )
        # Pydantic model, dict, list, str, number — JSON-encode.
        try:
            if hasattr(item, "model_dump"):
                data_str = _json.dumps(item.model_dump(by_alias=True))
            else:
                data_str = _json.dumps(item)
        except (TypeError, ValueError):
            data_str = _json.dumps(str(item))
        if format_sse_event is not None:
            return format_sse_event(data_str=data_str)
        return f"data: {data_str}\n\n".encode("utf-8")


class ORJSONResponse(Response):
    """JSON response using orjson for serialization (with stdlib json fallback)."""

    media_type = "application/json"

    def __init__(self, *args, **kwargs):
        import warnings as _warnings
        from fastapi_rs.exceptions import FastAPIDeprecationWarning as _W
        _warnings.warn(
            "ORJSONResponse is deprecated and will be removed in a future "
            "version. Use JSONResponse with orjson serialization instead.",
            _W,
            stacklevel=2,
        )
        super().__init__(*args, **kwargs)

    def render(self, content) -> bytes:
        try:
            import orjson
            # FA parity: ``OPT_NON_STR_KEYS`` lets handlers return dicts
            # with non-string keys (e.g. ``quoted_name`` from SQLAlchemy,
            # integers) — same behaviour as fastapi.responses.ORJSONResponse.
            return orjson.dumps(
                content,
                default=float,
                option=orjson.OPT_NON_STR_KEYS,
            )
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
