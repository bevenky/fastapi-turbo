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

# Real FastAPI's param classes. Imported during package init BEFORE
# ``compat.install()`` patches attributes onto the real package, so this binds
# the GENUINE classes (``fastapi.params`` is never patched). Each marker below
# multiply-inherits from the matching ``_real_params.*`` so REAL FastAPI
# introspection (the pivot adapter) recognizes it (correct ``in_`` / Body class),
# while the clone's ``_introspect`` keeps reading our custom attrs (``_kind`` …).
import fastapi.params as _real_params
from starlette.datastructures import UploadFile as _RealUploadFile
from pydantic.fields import FieldInfo


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
        # FA emits ``FastAPIDeprecationWarning`` when ``example=`` is
        # supplied — tests
        # (e.g. ``test_schema_extra_examples::test_openapi_schema``)
        # assert on this warning firing at decoration time.
        if example is not None:
            import warnings as _warnings
            from fastapi_turbo.exceptions import (
                FastAPIDeprecationWarning as _FADeprecationWarning,
            )
            _warnings.warn(
                "`example` has been deprecated, please use `examples` instead",
                _FADeprecationWarning,
                stacklevel=4,
            )
        # pattern is the modern name; regex is the legacy alias.
        # Emit the same deprecation warning FA does — test suites that
        # assert ``pytest.warns(FastAPIDeprecationWarning)`` depend on
        # it firing the moment a handler is decorated.
        if regex is not None:
            import warnings as _warnings
            from fastapi_turbo.exceptions import (
                FastAPIDeprecationWarning as _FADeprecationWarning,
            )
            _warnings.warn(
                "`regex` has been deprecated, please use `pattern` instead",
                _FADeprecationWarning,
                stacklevel=4,
            )

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

        # Store the clone's custom attrs AFTER super().__init__ — real FastAPI's
        # marker __init__ (now a base class) sets ``example``/``openapi_examples``
        # to DefaultPlaceholder/Undefined sentinels that the clone's OpenAPI
        # serializer chokes on, so our plain values must win.
        self.include_in_schema = include_in_schema
        self.example = example
        self.regex = pattern or regex
        self.pattern = self.regex
        self.openapi_examples = openapi_examples

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


class Param(_ParamMarker, _real_params.Param):
    pass


class Query(_ParamMarker, _real_params.Query):
    _kind = "query"


class Path(_ParamMarker, _real_params.Path):
    _kind = "path"


class Header(_ParamMarker, _real_params.Header):
    _kind = "header"

    def __init__(self, default=..., *, convert_underscores: bool = True, **kwargs):
        super().__init__(default, **kwargs)
        self.convert_underscores = convert_underscores


class Cookie(_ParamMarker, _real_params.Cookie):
    _kind = "cookie"


class Body(_ParamMarker, _real_params.Body):
    _kind = "body"

    def __init__(self, default=..., *, embed: bool | None = None, media_type: str = "application/json", **kwargs):
        super().__init__(default, **kwargs)
        # None means "auto-detect" (embed if multiple body params, else not).
        # Matches FastAPI's Body(embed=...) default.
        self.embed = embed
        self.media_type = media_type


class Form(_ParamMarker, _real_params.Form):
    _kind = "form"

    def __init__(self, default=..., *, media_type: str = "application/x-www-form-urlencoded", **kw):
        super().__init__(default, **kw)
        self.media_type = media_type


class File(_ParamMarker, _real_params.File):
    _kind = "file"


class UploadFile(_RealUploadFile, metaclass=ABCMeta):
    """File upload object matching FastAPI/Starlette's UploadFile interface.

    SUBCLASSES real ``starlette.datastructures.UploadFile`` so that
    ``issubclass(UploadFile, real UploadFile)`` is True — real FastAPI's
    ``get_dependant`` / ``get_openapi`` then recognize a ``f: UploadFile`` param
    as a file upload (the adapter / the real-OpenAPI pivot need this). The
    installed Starlette's ``UploadFile`` lacks ``__get_pydantic_core_schema__``,
    so real ``create_model_field`` can't build a field for it directly — we add
    that schema (below) on the subclass.

    The Rust multipart parser returns a ``PyUploadFile`` directly (the actual
    object handed to handlers); ``__subclasshook__`` makes
    ``isinstance(rust_upload_file, UploadFile)`` True via duck typing (it also
    covers any pre-shim real UploadFile instance). This class is additionally
    usable manually for testing. Bytes are held in-memory (axum buffers the whole
    request); read-cursor is independent per instance.
    """

    # __subclasshook__ makes isinstance(rust_upload_file, UploadFile) return True
    # whenever the object quacks like an UploadFile (has filename + read method).
    @classmethod
    def __subclasshook__(cls, other):
        if cls is not UploadFile:
            return NotImplemented
        # REAL Starlette/FastAPI UploadFile (sub)classes count as instances of
        # the turbo class. Post shim-flip the real form parsers keep their
        # load-time binding and build REAL UploadFile objects, while
        # ``starlette.datastructures.UploadFile`` (the name real
        # ``FormData.close`` isinstance-checks through at call time) is
        # patched to THIS class — without this branch those parsed uploads
        # would silently stop matching (and stop being closed). hasattr on
        # the real CLASS can't catch this: ``filename`` is a plain instance
        # attribute there.
        if isinstance(other, type) and issubclass(other, _RealUploadFile):
            return True
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
        # Do NOT call real ``__init__`` (it requires a SpooledTemporaryFile and
        # derives content_type from headers). Set the clone's attrs directly.
        self.filename = filename
        self.file = file
        self._content_type = content_type
        self.size = size
        self.headers = headers or {}

    # Real Starlette exposes ``content_type`` as a read-only property derived from
    # headers; the clone keeps it an explicit, settable value (the Rust
    # PyUploadFile / manual construction pass it directly), so override with a
    # settable property over ``_content_type``.
    @property
    def content_type(self):
        return self._content_type

    @content_type.setter
    def content_type(self, value):
        self._content_type = value

    async def read(self, size: int = -1) -> bytes:
        if self.file is not None:
            # Rust-side PyUploadFile exposes a sync read that returns bytes.
            data = self.file.read(size)
            return data
        return b""

    async def write(self, data: bytes) -> None:
        if self.size is not None:
            self.size += len(data)
        if hasattr(self.file, "write"):
            self.file.write(data)

    async def seek(self, offset: int) -> None:
        if self.file is not None and hasattr(self.file, "seek"):
            self.file.seek(offset)

    async def close(self) -> None:
        if self.file is not None and hasattr(self.file, "close"):
            self.file.close()

    @classmethod
    def _validate(cls, v, _info=None):
        """Starlette-parity pydantic validator. Reject non-UploadFile
        inputs with ``ValueError`` — used by ``test_upload_file_invalid_pydantic_v2``.
        """
        if not isinstance(v, UploadFile):
            raise ValueError(f"Expected UploadFile, received: {type(v)}")
        return v

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler):
        from pydantic_core import core_schema
        return core_schema.no_info_plain_validator_function(lambda v: v)

    @classmethod
    def __get_pydantic_json_schema__(cls, schema, handler):
        # OpenAPI 3.1 form (the default) — ``contentMediaType`` not the 3.0
        # ``format: binary``. Real ``get_openapi`` (the OpenAPI pivot) passes this
        # through verbatim; the clone ``_openapi.py`` hardcodes the file-param
        # schema separately (``_build_form_file_body``) so its output is unchanged.
        return {"type": "string", "contentMediaType": "application/octet-stream"}
