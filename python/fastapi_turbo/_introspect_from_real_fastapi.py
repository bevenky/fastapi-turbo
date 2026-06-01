"""Adapter: build the Rust engine's ``ParamInfo`` list from a REAL FastAPI route.

The "accelerate real FastAPI" pivot drives the Rust hot path off real FastAPI's
own introspection. Real FastAPI has ALREADY classified every parameter into
``route.dependant.{path,query,header,cookie,body}_params`` plus recursive
``.dependencies`` — so this module is a **mapper** (real bucket → ParamInfo), not
the ~1,500-line classifier the clone needed.

It produces the FLAT, topologically-ordered param list the Rust engine resolves
(``src/router.rs``): extraction params (``is_handler_param=False`` for dep-only
inputs) come before the ``"dependency"`` entries that consume them; each dep entry
carries ``dep_input_names=[(call_arg, source_key)]`` wiring its inputs from the
``resolved`` dict, and ``dep_callable_id`` (when ``use_cache``) so the engine
dedups shared deps.

Coverage is bounded to the common hot-path cases — scalar path/query/header/cookie
params, a single Pydantic/scalar JSON body, and (sync or async) NON-generator
dependencies whose own params are likewise scalars (recursively). Anything else —
``yield`` dependencies, deps with body/Form params, special params
(Request/Response/BackgroundTasks/SecurityScopes), Form/File, multiple/embedded
bodies — raises :class:`Undelegable`, and the caller delegates to real FastAPI.
``kind`` strings are the SHORT form the Rust extractor matches.
"""

from __future__ import annotations

import inspect
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
    try:
        from pydantic import TypeAdapter

        return TypeAdapter(ann)
    except Exception:
        return None


def _alias_of(field_info: Any) -> str | None:
    a = (
        getattr(field_info, "validation_alias", None)
        or getattr(field_info, "alias", None)
        or None
    )
    return a if isinstance(a, (str, type(None))) else None


# Scalar param buckets shared by handlers and dependencies (NOT body — handled
# separately — and NOT special params, which trigger a decline).
_SCALAR_BUCKETS = ("path", "query", "header", "cookie")


def _bucket_fields(dep: Any, bucket: str) -> list:
    return list(getattr(dep, f"{bucket}_params", []) or [])


def _param_from_field(
    mf: Any, kind: str, *, name: str | None = None, is_handler_param: bool = True
) -> ParamInfo:
    """Map one ModelField → ParamInfo.

    For a dependency's own input param pass an explicit ``name`` (a unique
    ``resolved`` key): the value is still EXTRACTED by the real request key
    (explicit alias, else the param's own name), but stored under ``name`` so it
    never collides with a handler param of the same name, and ``is_handler_param``
    is forced False (it feeds a dep, not the handler).
    """
    fi = mf.field_info
    ann = fi.annotation
    required = fi.is_required()
    is_body_model = kind == "body" and _is_basemodel(ann)
    explicit_alias = _alias_of(fi)
    if name is not None:
        resolved_name = name
        alias = explicit_alias or mf.name  # carry the real lookup key
        is_handler_param = False
    else:
        resolved_name = mf.name
        alias = explicit_alias
    return ParamInfo(
        name=resolved_name,
        kind=kind,
        type_hint=("model" if is_body_model else _get_type_name(ann)),
        required=required,
        default_value=_default_of(fi),
        has_default=not required,
        model_class=(ann if is_body_model else None),
        alias=alias,
        scalar_validator=(None if is_body_model else _scalar_validator(ann)),
        is_handler_param=is_handler_param,
    )


def _check_special(dep: Any) -> None:
    """Decline a dependant (handler or dep) that uses a special param the buffered
    Rust door can't host."""
    for special in (
        "request_param_name",
        "response_param_name",
        "background_tasks_param_name",
        "security_scopes_param_name",
    ):
        if getattr(dep, special, None) is not None:
            raise Undelegable(f"uses {special} → real FastAPI")


def _emit_dep(dep: Any, out: list[ParamInfo], uid: str, *, is_handler_param: bool) -> str:
    """Emit the flat params for one dependency (post-order: its own inputs +
    sub-deps first, then the ``"dependency"`` entry). Returns the ``resolved`` key
    holding the dep's result, so a parent can wire it as an input."""
    _check_special(dep)
    call = dep.call
    if inspect.isgeneratorfunction(call) or inspect.isasyncgenfunction(call):
        raise Undelegable("yield/generator dependency → real FastAPI")
    if _bucket_fields(dep, "body"):
        raise Undelegable("dependency with a body param → real FastAPI")

    input_wiring: list[tuple[str, str]] = []

    # 1. the dependency's own scalar params — extracted, fed to the dep by name.
    for kind in _SCALAR_BUCKETS:
        for mf in _bucket_fields(dep, kind):
            src_key = f"_dep{uid}__{mf.name}"
            out.append(_param_from_field(mf, kind, name=src_key))
            input_wiring.append((mf.name, src_key))

    # 2. sub-dependencies first (so they resolve before this dep), wired by the
    #    parent param name the sub-dep result feeds.
    for k, sub in enumerate(dep.dependencies):
        sub_key = _emit_dep(sub, out, f"{uid}_{k}", is_handler_param=False)
        input_wiring.append((sub.name, sub_key))

    # 3. the dependency itself.
    result_key = dep.name if (is_handler_param and dep.name) else f"_dep{uid}"
    out.append(
        ParamInfo(
            name=result_key,
            kind="dependency",
            type_hint="any",
            required=True,
            default_value=None,
            has_default=False,
            model_class=None,
            alias=None,
            dep_callable=call,
            dep_callable_id=(id(call) if getattr(dep, "use_cache", True) else None),
            is_async_dep=inspect.iscoroutinefunction(call),
            is_generator_dep=False,
            dep_input_names=input_wiring,
            is_handler_param=is_handler_param,
            scalar_validator=None,
        )
    )
    return result_key


def extract_params_from_route(route: Any) -> list[ParamInfo]:
    """Map a real FastAPI ``APIRoute``'s ``route.dependant`` to the Rust engine's
    flat ``ParamInfo`` list. Raises :class:`Undelegable` for surface the Rust door
    does not cover, so the caller falls back to real FastAPI."""
    dep = route.dependant
    _check_special(dep)

    params: list[ParamInfo] = []

    # Handler's own scalar params.
    for kind in _SCALAR_BUCKETS:
        for mf in _bucket_fields(dep, kind):
            params.append(_param_from_field(mf, kind))

    # Handler's body (single JSON body only; Form/File and multiple bodies delegate).
    body_fields = _bucket_fields(dep, "body")
    if body_fields:
        if len(body_fields) != 1:
            raise Undelegable("multiple/embedded body params → real FastAPI")
        bf = body_fields[0]
        if type(bf.field_info).__name__ in ("Form", "File"):
            raise Undelegable("Form/File body → real FastAPI")
        params.append(_param_from_field(bf, "body"))

    # Handler's dependencies (top-level → handler params).
    for i, sub in enumerate(dep.dependencies):
        _emit_dep(sub, params, str(i), is_handler_param=True)

    return params
