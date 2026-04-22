"""Minimal ``fastapi._compat`` shim for third-party plugins.

FastAPI exposes helpers in ``fastapi._compat`` that abstract over Pydantic
v1/v2 differences.  Since fastapi-rs requires Pydantic v2, these are thin
wrappers that delegate directly to the v2 API.
"""

from __future__ import annotations

from typing import Any


def _model_dump(
    model: Any,
    *,
    mode: str = "python",
    include: Any = None,
    exclude: Any = None,
    by_alias: bool = False,
    exclude_unset: bool = False,
    exclude_defaults: bool = False,
    exclude_none: bool = False,
) -> dict[str, Any]:
    """Dump a Pydantic v2 model to a dict."""
    return model.model_dump(
        mode=mode,
        include=include,
        exclude=exclude,
        by_alias=by_alias,
        exclude_unset=exclude_unset,
        exclude_defaults=exclude_defaults,
        exclude_none=exclude_none,
    )


def _model_rebuild(model: type) -> None:
    """Rebuild a Pydantic v2 model (resolves forward refs)."""
    model.model_rebuild()


def _get_model_config(model: type) -> dict[str, Any]:
    """Return the model_config dict from a Pydantic v2 model."""
    return getattr(model, "model_config", {})


# Additional compat symbols that real fastapi._compat exports
ModelNameMap = dict
Undefined = ...  # Sentinel matching pydantic's PydanticUndefined

try:
    from pydantic import field_validator, model_validator  # noqa: F401
except ImportError:
    pass

try:
    from pydantic_core import PydanticUndefined as Undefined  # type: ignore[assignment]  # noqa: F811
except ImportError:
    pass


# ── annotation classifiers ─────────────────────────────────────────
# FastAPI's private helpers that inspect parameter annotations. These
# are invoked by a handful of third-party plugins to decide "is this
# an UploadFile parameter?" etc. Keep them out of the hot path: they
# only run at app-startup / introspection time.

def _iter_optional_args(annotation: Any):
    """Yield each non-None arg of an ``Optional[...]`` / ``Union[...]``
    annotation. Plain types yield themselves; everything else yields
    the annotation as-is so callers can still run ``issubclass``.
    """
    import typing as _t
    origin = _t.get_origin(annotation)
    if origin is _t.Union:
        for a in _t.get_args(annotation):
            if a is type(None):
                continue
            yield a
    else:
        yield annotation


def is_uploadfile_or_nonable_uploadfile_annotation(annotation: Any) -> bool:
    try:
        from fastapi_rs.param_functions import UploadFile as _UF
    except Exception:
        return False
    for a in _iter_optional_args(annotation):
        if isinstance(a, type) and issubclass(a, _UF):
            return True
    return False


def is_uploadfile_sequence_annotation(annotation: Any) -> bool:
    """True for ``list[UploadFile]`` / ``tuple[UploadFile, ...]`` /
    ``set[UploadFile]`` and their ``Optional[...]`` wrappers."""
    try:
        from fastapi_rs.param_functions import UploadFile as _UF
    except Exception:
        return False
    import typing as _t
    for a in _iter_optional_args(annotation):
        origin = _t.get_origin(a)
        if origin in (list, tuple, set, frozenset):
            args = _t.get_args(a)
            if args:
                inner = args[0]
                if isinstance(inner, type) and issubclass(inner, _UF):
                    return True
    return False


def is_bytes_or_nonable_bytes_annotation(annotation: Any) -> bool:
    for a in _iter_optional_args(annotation):
        if isinstance(a, type) and issubclass(a, bytes):
            return True
    return False


def is_bytes_sequence_annotation(annotation: Any) -> bool:
    import typing as _t
    for a in _iter_optional_args(annotation):
        origin = _t.get_origin(a)
        if origin in (list, tuple, set, frozenset):
            args = _t.get_args(a)
            if args and isinstance(args[0], type) and issubclass(args[0], bytes):
                return True
    return False


def is_sequence_field(field: Any) -> bool:
    """Starlette parity: detect sequence-typed fields on model objects."""
    ann = getattr(getattr(field, "field_info", None), "annotation", None) or getattr(field, "annotation", None)
    if ann is None:
        return False
    import typing as _t
    for a in _iter_optional_args(ann):
        if _t.get_origin(a) in (list, tuple, set, frozenset):
            return True
    return False


def sequence_types():
    """Tuple of sequence classes FA's uploader recognises."""
    return (list, tuple, set, frozenset)


def value_is_sequence(value: Any) -> bool:
    return isinstance(value, (list, tuple, set, frozenset)) and not isinstance(value, (str, bytes))


def serialize_sequence_value(*, field: Any, value: Any) -> Any:
    """FA-parity: when a file field is a sequence of bytes, return the
    bytes list verbatim. fastapi-rs doesn't use this internally, but
    a plugin that calls it shouldn't crash.
    """
    return list(value) if value_is_sequence(value) else value
