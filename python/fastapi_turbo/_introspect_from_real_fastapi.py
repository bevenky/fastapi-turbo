"""Adapter: build the Rust engine's ``ParamInfo`` list from a REAL FastAPI route.

The "accelerate real FastAPI" pivot drives the Rust hot path off real FastAPI's
own introspection. Real FastAPI has ALREADY classified every parameter into
``route.dependant.{path,query,header,cookie,body}_params`` plus recursive
``.dependencies`` — so this module is a thin **mapper** (real bucket → ParamInfo),
not the ~1,500-line classifier the clone needed.

Coverage is deliberately bounded to the common hot-path cases: scalar
path/query/header/cookie params and a single Pydantic-model (or scalar) JSON body.
Anything outside that — sub-dependencies, special params (``Request``/``Response``/
``BackgroundTasks``/``SecurityScopes``), ``Form``/``File`` bodies, multiple/embedded
body params — raises :class:`Undelegable`, and the caller DECLINES so the request
flows to real Starlette via ``super().__call__()`` (correct by definition). The
``kind`` strings are the SHORT form the Rust extractor matches
(``path``/``query``/``header``/``cookie``/``body``/``dependency`` — see
``src/router.rs``).
"""

from __future__ import annotations

from typing import Any

from fastapi_turbo._fastapi_turbo_core import ParamInfo
from fastapi_turbo._introspect import _get_type_name

try:  # pydantic v2 sentinel for "no default"
    from pydantic_core import PydanticUndefined
except Exception:  # pragma: no cover
    from pydantic.fields import PydanticUndefined  # type: ignore


class Undelegable(Exception):
    """This route is outside the Rust door's coverage — delegate to real FastAPI."""


def _is_basemodel(ann: Any) -> bool:
    try:
        from pydantic import BaseModel

        return isinstance(ann, type) and issubclass(ann, BaseModel)
    except Exception:
        return False


def _default_of(field_info: Any) -> Any:
    d = getattr(field_info, "default", PydanticUndefined)
    return None if d is PydanticUndefined else d


def _scalar_validator(ann: Any):
    """A pydantic ``TypeAdapter`` the Rust path uses to coerce/validate a scalar.
    ``None`` if the annotation can't build one (Rust falls back to type_hint coercion)."""
    try:
        from pydantic import TypeAdapter

        return TypeAdapter(ann)
    except Exception:
        return None


def _alias_of(field_info: Any) -> str | None:
    # validation_alias wins for input binding when set (matches FastAPI).
    return (
        getattr(field_info, "validation_alias", None)
        or getattr(field_info, "alias", None)
        or None
    )


def _param_from_field(mf: Any, kind: str) -> ParamInfo:
    fi = mf.field_info
    ann = fi.annotation
    required = fi.is_required()
    is_body_model = kind == "body" and _is_basemodel(ann)
    return ParamInfo(
        name=mf.name,
        kind=kind,
        type_hint=("model" if is_body_model else _get_type_name(ann)),
        required=required,
        default_value=_default_of(fi),
        has_default=not required,
        model_class=(ann if is_body_model else None),
        alias=(None if isinstance(_alias_of(fi), property) else _alias_of(fi)),
        scalar_validator=(None if is_body_model else _scalar_validator(ann)),
    )


def extract_params_from_route(route: Any) -> list[ParamInfo]:
    """Map a real FastAPI ``APIRoute``'s ``route.dependant`` to Rust ``ParamInfo``.

    Raises :class:`Undelegable` for any surface the Rust door does not cover, so
    the caller can fall back to real FastAPI.
    """
    dep = route.dependant

    # Special params the buffered Rust door can't host → delegate.
    for special in ("request_param_name", "response_param_name",
                    "background_tasks_param_name", "security_scopes_param_name"):
        if getattr(dep, special, None) is not None:
            raise Undelegable(f"route uses {special} → real FastAPI")

    # Sub-dependencies are not mapped yet (Rust dep-plan reconciliation is a later
    # pivot step) → delegate any route with dependencies.
    if getattr(dep, "dependencies", None):
        raise Undelegable("route has dependencies → real FastAPI")

    params: list[ParamInfo] = []
    for kind, fields in (
        ("path", dep.path_params),
        ("query", dep.query_params),
        ("header", getattr(dep, "header_params", [])),
        ("cookie", getattr(dep, "cookie_params", [])),
    ):
        for mf in fields:
            params.append(_param_from_field(mf, kind))

    body_fields = list(getattr(dep, "body_params", []))
    if body_fields:
        # Only a single JSON body param is covered; Form/File and multiple/embedded
        # bodies delegate. FastAPI marks form bodies with FieldInfo subclasses
        # (Form/File) — detect by the marker class name to stay version-robust.
        if len(body_fields) != 1:
            raise Undelegable("multiple/embedded body params → real FastAPI")
        bf = body_fields[0]
        marker = type(bf.field_info).__name__
        if marker in ("Form", "File"):
            raise Undelegable("Form/File body → real FastAPI")
        params.append(_param_from_field(bf, "body"))

    return params
