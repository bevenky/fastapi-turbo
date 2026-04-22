"""Parameter marker classes for controlling how handler parameters are extracted.

These match FastAPI's API: Query, Path, Header, Cookie, Body, Form, File.
Users set these as default values in handler signatures to control extraction.

Inheriting from pydantic.fields.FieldInfo ensures that
``Annotated[int, Query(ge=0)]`` integrates with Pydantic's validation
pipeline automatically (the FieldInfo metadata is picked up by
BaseModel field resolution).
"""

from __future__ import annotations

from abc import ABCMeta

from pydantic.fields import FieldInfo


# Keys that we handle ourselves and must NOT be forwarded to FieldInfo.__init__
# (FieldInfo silently accepts them but discards the values).
_CUSTOM_KEYS = frozenset({
    "example",       # singular example (FieldInfo only has 'examples')
    "regex",         # legacy alias for 'pattern'
    "include_in_schema",  # not a FieldInfo kwarg
})


class _ParamMarker(FieldInfo):
    """Base for all parameter markers.

    Inherits from ``pydantic.fields.FieldInfo`` so that instances are
    recognised by Pydantic when used inside ``Annotated[T, Query(...)]``.
    Custom attributes (``_kind``, ``example``, ``regex``,
    ``include_in_schema``) are stored on the instance directly.
    """

    _kind: str = ""  # "query", "header", "cookie", "path", "body", "form", "file"

    def __init__(
        self,
        default=...,
        *,
        alias: str | None = None,
        validation_alias: str | None = None,
        serialization_alias: str | None = None,
        alias_priority: int | None = None,
        title: str | None = None,
        description: str | None = None,
        gt=None,
        ge=None,
        lt=None,
        le=None,
        min_length: int | None = None,
        max_length: int | None = None,
        regex: str | None = None,
        pattern: str | None = None,
        example=None,
        examples=None,
        openapi_examples=None,
        deprecated: bool | None = None,
        include_in_schema: bool = True,
        json_schema_extra=None,
        **extra,
    ):
        # Store custom attrs that FieldInfo doesn't natively keep
        self.include_in_schema = include_in_schema
        self.example = example
        # pattern is the modern name; regex is the legacy alias.
        # Emit the same deprecation warning FA does — test suites that
        # assert ``pytest.warns(FastAPIDeprecationWarning)`` depend on
        # it firing the moment a handler is decorated.
        if regex is not None:
            import warnings as _warnings
            from fastapi_rs.exceptions import (
                FastAPIDeprecationWarning as _FADeprecationWarning,
            )
            _warnings.warn(
                "`regex` has been deprecated, please use `pattern` instead",
                _FADeprecationWarning,
                stacklevel=4,
            )
        self.regex = pattern or regex
        self.pattern = self.regex
        self.openapi_examples = openapi_examples

        # Build kwargs for FieldInfo.__init__. Pydantic's ``Field(...)``
        # implicitly propagates ``alias`` to ``validation_alias`` and
        # ``serialization_alias`` when the latter two aren't passed —
        # tests that do ``Form(alias="p_alias")`` and assert on
        # ``schema.properties["p_alias"]`` depend on this (Pydantic's
        # schema generator uses ``serialization_alias`` for output).
        fi_kwargs: dict = {}
        if alias is not None:
            fi_kwargs["alias"] = alias
            if validation_alias is None:
                fi_kwargs["validation_alias"] = alias
            if serialization_alias is None:
                fi_kwargs["serialization_alias"] = alias
        if validation_alias is not None:
            fi_kwargs["validation_alias"] = validation_alias
        if serialization_alias is not None:
            fi_kwargs["serialization_alias"] = serialization_alias
        if alias_priority is not None:
            fi_kwargs["alias_priority"] = alias_priority
        if title is not None:
            fi_kwargs["title"] = title
        if description is not None:
            fi_kwargs["description"] = description
        if gt is not None:
            fi_kwargs["gt"] = gt
        if ge is not None:
            fi_kwargs["ge"] = ge
        if lt is not None:
            fi_kwargs["lt"] = lt
        if le is not None:
            fi_kwargs["le"] = le
        if min_length is not None:
            fi_kwargs["min_length"] = min_length
        if max_length is not None:
            fi_kwargs["max_length"] = max_length
        # 'regex' is the legacy name; Pydantic v2 uses 'pattern'
        effective_pattern = pattern or regex
        if effective_pattern is not None:
            fi_kwargs["pattern"] = effective_pattern
        if examples is not None:
            fi_kwargs["examples"] = examples
        if deprecated is not None:
            fi_kwargs["deprecated"] = deprecated
        if json_schema_extra is not None:
            fi_kwargs["json_schema_extra"] = json_schema_extra

        super().__init__(default=default, **fi_kwargs, **extra)

    def __repr__(self) -> str:
        # FastAPI's param classes use a minimal repr that just shows
        # the default value. Tests (and some user debug output) assert
        # on this exact form: ``Query(teststr)``, ``Body(...)``, etc.
        from pydantic_core import PydanticUndefined as _Und
        default = self.default
        if default is _Und or default is Ellipsis:
            default_repr = "PydanticUndefined"
        else:
            default_repr = str(default)
        return f"{type(self).__name__}({default_repr})"


class Param(_ParamMarker):
    pass


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

    def __init__(self, default=..., *, media_type: str = "application/x-www-form-urlencoded", **kw):
        super().__init__(default, **kw)
        self.media_type = media_type


class File(_ParamMarker):
    _kind = "file"


class UploadFile(metaclass=ABCMeta):
    """File upload object matching FastAPI/Starlette's UploadFile interface.

    The Rust multipart parser returns a PyUploadFile directly — this Python
    class is (a) usable manually for testing, (b) registered as a virtual
    superclass of PyUploadFile so ``isinstance(f, UploadFile)`` works.

    The underlying bytes are held in-memory (axum buffers the whole request).
    Read-cursor is independent per instance; seek/tell work as expected.
    """

    # __subclasshook__ makes isinstance(rust_upload_file, UploadFile) return True
    # whenever the object quacks like an UploadFile (has filename + read method).
    @classmethod
    def __subclasshook__(cls, other):
        if cls is not UploadFile:
            return NotImplemented
        if all(hasattr(other, attr) for attr in ("filename", "content_type", "read")):
            return True
        return NotImplemented

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
        if self.file is not None:
            # Rust-side PyUploadFile exposes a sync read that returns bytes.
            data = self.file.read(size)
            return data
        return b""

    async def write(self, data: bytes) -> None:
        if hasattr(self.file, "write"):
            self.file.write(data)

    async def seek(self, offset: int) -> None:
        if self.file is not None and hasattr(self.file, "seek"):
            self.file.seek(offset)

    async def close(self) -> None:
        if self.file is not None and hasattr(self.file, "close"):
            self.file.close()

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler):
        from pydantic_core import core_schema
        return core_schema.no_info_plain_validator_function(lambda v: v)

    @classmethod
    def __get_pydantic_json_schema__(cls, schema, handler):
        return {"type": "string", "format": "binary"}
