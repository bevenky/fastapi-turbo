"""Parameter marker classes for controlling how handler parameters are extracted.

These match FastAPI's API: Query, Path, Header, Cookie, Body, Form, File.
Users set these as default values in handler signatures to control extraction.
"""

from __future__ import annotations


class _ParamMarker:
    """Base for all parameter markers."""

    _kind: str = ""  # "query", "header", "cookie", "path", "body", "form", "file"

    def __init__(
        self,
        default=...,
        *,
        alias: str | None = None,
        title: str | None = None,
        description: str | None = None,
        gt=None,
        ge=None,
        lt=None,
        le=None,
        min_length: int | None = None,
        max_length: int | None = None,
        regex: str | None = None,
        example=None,
        examples=None,
        deprecated: bool | None = None,
        include_in_schema: bool = True,
        json_schema_extra=None,
        **extra,
    ):
        self.default = default
        self.alias = alias
        self.title = title
        self.description = description
        self.gt = gt
        self.ge = ge
        self.lt = lt
        self.le = le
        self.min_length = min_length
        self.max_length = max_length
        self.regex = regex
        self.example = example
        self.examples = examples
        self.deprecated = deprecated
        self.include_in_schema = include_in_schema
        self.json_schema_extra = json_schema_extra


class Query(_ParamMarker):
    _kind = "query"


class Path(_ParamMarker):
    _kind = "path"


class Header(_ParamMarker):
    _kind = "header"

    def __init__(self, default=..., *, convert_underscores: bool = True, **kwargs):
        super().__init__(default, **kwargs)
        self.convert_underscores = convert_underscores


class Cookie(_ParamMarker):
    _kind = "cookie"


class Body(_ParamMarker):
    _kind = "body"

    def __init__(self, default=..., *, embed: bool | None = None, media_type: str = "application/json", **kwargs):
        super().__init__(default, **kwargs)
        # None means "auto-detect" (embed if multiple body params, else not).
        # Matches FastAPI's Body(embed=...) default.
        self.embed = embed
        self.media_type = media_type


class Form(_ParamMarker):
    _kind = "form"


class File(_ParamMarker):
    _kind = "file"


class UploadFile:
    """Stub for file upload objects, matching FastAPI's interface."""

    def __init__(
        self,
        filename: str | None = None,
        file=None,
        content_type: str | None = None,
        *,
        size: int | None = None,
        headers=None,
    ):
        self.filename = filename
        self.file = file
        self.content_type = content_type
        self.size = size
        self.headers = headers or {}

    async def read(self, size: int = -1) -> bytes:
        if self.file:
            return self.file.read(size)
        return b""

    async def write(self, data: bytes) -> None:
        if self.file:
            self.file.write(data)

    async def seek(self, offset: int) -> None:
        if self.file:
            self.file.seek(offset)

    async def close(self) -> None:
        if self.file:
            self.file.close()
