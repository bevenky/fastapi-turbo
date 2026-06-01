"""Door-support helpers — the slice of introspection the RUST DOOR and the pivot
adapter depend on, relocated out of the (clone) _introspect.py so the clone module
becomes deletable and the adapter no longer imports from it (breaks a circular
import). KEPT permanently: the Rust engine calls _make_fa_body_validator at route
build, and the adapter + clone introspection call _get_type_name / _is_union_origin
/ _TypeAdapterProxy / _FABodyValidator.
"""

from __future__ import annotations

import inspect
import re
import types
import typing
from typing import Any


def _is_union_origin(origin: Any) -> bool:
    return origin is typing.Union or origin is types.UnionType

def _get_type_name(annotation) -> str:
    """Map a type annotation to a simple string the Rust side understands.

    Returned values:
      - "int", "float", "bool", "str", "bytes"  — scalar coercion
      - "list_int", "list_float", "list_bool", "list_str"  — multi-value
        query/form/header repetition collected into a Python list. The
        downstream router uses ``_get_container_type`` on the same
        annotation to decide whether to wrap the list in ``set`` /
        ``frozenset`` / ``tuple`` before handing it to the handler.
    """
    if annotation is inspect.Parameter.empty or annotation is None:
        return "str"

    origin = typing.get_origin(annotation) or getattr(annotation, "__origin__", None)
    if origin is not None:
        # Optional[X] / Union[X, None] — strip None, recurse
        if _is_union_origin(origin):
            args = [a for a in annotation.__args__ if a is not type(None)]
            if args:
                return _get_type_name(args[0])
            return "str"
        # list[X] / List[X] / tuple[X, ...] / set[X] / frozenset[X]
        if origin in (list, tuple, set, frozenset):
            args = typing.get_args(annotation) or getattr(annotation, "__args__", ())
            inner = args[0] if args else str
            inner_name = _get_type_name(inner)
            return f"list_{inner_name}"

    # Bare ``list`` / ``tuple`` / ``set`` / ``frozenset`` (without generic
    # args) — treat as ``list_str`` so multiple query/form/header values
    # are collected into a Python list. Matches FA's behaviour for
    # ``q: Annotated[list, Query()] = []``.
    if annotation in (list, tuple, set, frozenset):
        return "list_str"

    type_map: dict[type, str] = {
        int: "int",
        float: "float",
        bool: "bool",
        str: "str",
        bytes: "bytes",
    }

    if isinstance(annotation, type):
        # Enum subclass — return the base type so Rust extracts correctly,
        # but the enum_class will be stored separately for Python-side coercion.
        import enum as _enum_mod
        if issubclass(annotation, _enum_mod.Enum):
            for base in annotation.__mro__:
                if base in type_map:
                    return type_map[base]
            return "str"
        return type_map.get(annotation, "str")

    return "str"

class _TypeAdapterProxy:
    """Minimal shim that exposes a Pydantic TypeAdapter's validator via
    `__pydantic_validator__`. The Rust body extractor caches that attribute
    at startup and calls `.validate_json(bytes)` on it per request, so this
    proxy lets non-BaseModel body types (`list[Item]`, `dict[str, X]`, …)
    follow the same hot path as a regular BaseModel.
    """

    __slots__ = (
        "_ta",
        "__pydantic_validator__",
        "__pydantic_serializer__",
        "_annotation",
    )

    def __init__(self, annotation):
        from pydantic import TypeAdapter

        self._annotation = annotation
        self._ta = TypeAdapter(annotation)
        self.__pydantic_validator__ = self._ta.validator
        self.__pydantic_serializer__ = self._ta.serializer

    def model_validate(self, value):
        return self._ta.validate_python(value)

class _FABodyValidator:
    """FA-compatible body validator: parses JSON bytes then calls
    ``validator.validate_python(data, from_attributes=True)`` on a
    TypeAdapter wrapped in ``Annotated[T, FieldInfo(annotation=T)]``.

    This mirrors how stock FastAPI runs body validation and yields the
    exact error shape (``model_attributes_type`` instead of
    ``model_type``, "Input should be a valid dictionary or object to
    extract fields from" messages, no ``ctx.class_name`` field, …).
    Exposes ``validate_json`` so the Rust router can call it
    polymorphically alongside Pydantic's own SchemaValidator.
    """

    __slots__ = ("_validator", "_is_model", "_combined_body_fields")

    def __init__(self, annotation, combined_body_fields=None):
        from pydantic import TypeAdapter
        from pydantic.fields import FieldInfo
        from typing import Annotated as _Annotated

        try:
            from pydantic import BaseModel as _BM
            is_model = isinstance(annotation, type) and issubclass(annotation, _BM)
        except ImportError:
            is_model = False

        if is_model:
            ta = TypeAdapter(_Annotated[annotation, FieldInfo(annotation=annotation)])
        else:
            ta = TypeAdapter(annotation)
        self._validator = ta.validator
        self._is_model = is_model
        # When set, this is a ``_combined_body`` wrapper — a list of
        # field names that produce per-field ``missing`` errors when
        # the raw body isn't a dict (FA parity — ``PUT`` with body
        # ``[]`` on a multi-body endpoint emits one missing per
        # required param, not ``model_attributes_type``).
        self._combined_body_fields = combined_body_fields

    def _maybe_combined_body_missing(self, data) -> None:
        if not self._combined_body_fields or isinstance(data, dict):
            return
        from pydantic_core import InitErrorDetails, PydanticCustomError
        from pydantic import ValidationError
        details = [
            InitErrorDetails(
                type=PydanticCustomError("missing", "Field required", {}),
                loc=(fname,),
                input=None,
            )
            for fname in self._combined_body_fields
        ]
        raise ValidationError.from_exception_data(
            "combined_body_missing", details
        ) from None

    def validate_json(self, body):
        import json as _json

        if isinstance(body, (bytes, bytearray, memoryview)):
            raw = bytes(body)
        elif isinstance(body, str):
            raw = body.encode("utf-8")
        else:
            self._maybe_combined_body_missing(body)
            return self._validator.validate_python(body, from_attributes=True)
        try:
            data = _json.loads(raw)
        except Exception as e:
            # FA parity: a non-JSONDecodeError from json.loads (e.g. a
            # test patch raising a bare Exception) still maps to a 400
            # "There was an error parsing the body" response — FastAPI's
            # request body handler catches the generic exception the
            # same way.
            if not isinstance(e, _json.JSONDecodeError):
                from fastapi_turbo.exceptions import HTTPException as _HE
                raise _HE(status_code=400, detail="There was an error parsing the body") from None
            # Match FastAPI: emit a single `json_invalid` error with
            # loc=("body", byte_pos) and ctx.error = Python's json
            # message. We raise a synthetic ValidationError so the Rust
            # error formatter handles it uniformly.
            from pydantic_core import InitErrorDetails, PydanticCustomError
            from pydantic import ValidationError
            raise ValidationError.from_exception_data(
                "json_invalid",
                [
                    InitErrorDetails(
                        type=PydanticCustomError(
                            "json_invalid",
                            "JSON decode error",
                            {"error": e.msg},
                        ),
                        loc=(e.pos,),
                        input={},
                    )
                ],
            ) from None
        self._maybe_combined_body_missing(data)
        return self._validator.validate_python(data, from_attributes=True)

    def validate_python(self, value):
        self._maybe_combined_body_missing(value)
        return self._validator.validate_python(value, from_attributes=True)

def _make_fa_body_validator(annotation, combined_body_fields=None):
    # If a ``_TypeAdapterProxy`` was handed in (set by the body-type
    # resolver for ``list[Item]`` / ``dict[str, X]`` / scalar body),
    # unwrap it back to the raw annotation so the FA-style adapter is
    # built on the real type.
    if isinstance(annotation, _TypeAdapterProxy):
        annotation = annotation._annotation
    # If the model is a combined-body wrapper (built by
    # ``_maybe_embed_body_params``), pick up its required field names so
    # validation emits per-field ``missing`` errors instead of a single
    # ``model_attributes_type`` when the raw body isn't a dict.
    if combined_body_fields is None:
        fields = getattr(annotation, "_fastapi_turbo_combined_body_fields", None)
        if fields:
            combined_body_fields = tuple(fields)
    try:
        return _FABodyValidator(annotation, combined_body_fields=combined_body_fields)
    except Exception:  # noqa: BLE001
        return None
