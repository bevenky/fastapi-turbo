"""Adapter: build the Rust engine's ``ParamInfo`` list from a REAL FastAPI route.

The "accelerate real FastAPI" pivot drives the Rust hot path off real FastAPI's
own introspection. Real FastAPI has ALREADY classified every parameter into
``route.dependant.{path,query,header,cookie,body}_params`` plus recursive
``.dependencies`` and the special-param slots
(``request_param_name``/``response_param_name``/…) — so this module is a
**mapper** (real bucket → ParamInfo), not the ~2,000-line classifier the clone
needed.

It produces the FLAT, topologically-ordered param list the Rust engine resolves
(``src/router.rs``): extraction params (``is_handler_param=False`` for dep-only
inputs) come before the ``"dependency"`` entries that consume them; each dep entry
carries ``dep_input_names=[(call_arg, source_key)]`` wiring its inputs from the
``resolved`` dict, and ``dep_callable_id`` (when ``use_cache``) so the engine
dedups shared deps.

Coverage maps the common hot-path surface onto the Rust ``kind`` vocabulary the
door already implements:

* scalar path/query/header/cookie params (constraints included — the validator is
  real FastAPI's own ``ModelField._type_adapter``);
* a single Pydantic/scalar JSON body;
* ``Form`` / ``File`` multipart & urlencoded bodies (``form`` / ``file`` kinds);
* special params — ``Request``/``HTTPConnection`` → ``inject_request``,
  ``Response`` → ``inject_response``, ``BackgroundTasks`` →
  ``inject_background_tasks``, ``SecurityScopes`` → ``inject_security_scopes``;
* ``response_model`` filtering — :func:`build_handler` wraps the endpoint so the
  result is run through real FastAPI's own ``ModelField.validate``/``.serialize``.
* (sync or async) NON-generator dependencies whose own params are scalars.

Anything outside that — ``yield`` dependencies, deps that take a body or a special
param, ``Form`` model-expansion, multiple/embedded JSON bodies — raises
:class:`Undelegable`, and the caller delegates to real FastAPI.
``kind`` strings are the SHORT form the Rust extractor matches.
"""

from __future__ import annotations

import inspect
import typing
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


def _unwrap_optional(ann: Any) -> Any:
    """``Optional[X]`` / ``Union[X, None]`` → ``X`` (else unchanged)."""
    origin = typing.get_origin(ann)
    if origin is typing.Union or (origin is not None and origin.__name__ == "UnionType"):
        non_none = [a for a in typing.get_args(ann) if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return ann


def _default_of(field_info: Any) -> Any:
    d = getattr(field_info, "default", PydanticUndefined)
    return None if d is PydanticUndefined else d


def _field_validator(mf: Any, is_body_model: bool, kind: str):
    """The pydantic validator the Rust door calls ``.validate_python`` on.

    Prefer real FastAPI's OWN ``ModelField._type_adapter`` — it bakes in the
    field's constraints (``Query(gt=…)``, ``regex=…`` etc.) so the door enforces
    them identically. Body models validate via ``model_class.model_validate`` and
    files are passed through untouched, so both skip the scalar validator.
    """
    if is_body_model or kind == "file":
        return None
    ta = getattr(mf, "_type_adapter", None)
    if ta is not None and hasattr(ta, "validate_python"):
        return ta
    fi = mf.field_info
    ann = fi.annotation
    try:  # fallback: rebuild from annotation + FieldInfo (carries constraints)
        from pydantic import TypeAdapter

        return TypeAdapter(typing.Annotated[ann, fi])
    except Exception:
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
# separately — and NOT special params, which are emitted/declined separately).
_SCALAR_BUCKETS = ("path", "query", "header", "cookie")

# Real FastAPI Dependant special-param slots → Rust injection kind.
_SPECIAL_PARAM_KINDS = (
    ("request_param_name", "inject_request"),
    ("http_connection_param_name", "inject_request"),
    ("response_param_name", "inject_response"),
    ("background_tasks_param_name", "inject_background_tasks"),
    ("security_scopes_param_name", "inject_security_scopes"),
)


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
    # FastAPI 0.115+ query/header/cookie parameter-MODELS (``f:
    # Annotated[Model, Query()]``) arrive as a single BaseModel-typed scalar
    # field that real FastAPI expands into one param per model field. The door
    # has no expansion, so delegate.
    if kind in _SCALAR_BUCKETS and _is_basemodel(_unwrap_optional(ann)):
        raise Undelegable(f"{kind} parameter-model expansion → real FastAPI")
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
        scalar_validator=_field_validator(mf, is_body_model, kind),
        is_handler_param=is_handler_param,
    )


def _check_special(dep: Any) -> None:
    """Decline a DEPENDENCY that uses a special param. The door injects framework
    objects into the handler's kwargs only — it cannot wire a Request/Response/etc.
    into a dependency's inputs — so such deps fall back to real FastAPI."""
    for attr, _kind in _SPECIAL_PARAM_KINDS:
        if getattr(dep, attr, None) is not None:
            raise Undelegable(f"dependency uses {attr} → real FastAPI")
    if getattr(dep, "websocket_param_name", None) is not None:
        raise Undelegable("dependency uses websocket_param_name → real FastAPI")


def _emit_special_params(dep: Any, out: list[ParamInfo]) -> None:
    """Emit ``inject_*`` ParamInfos for the handler's special params. The door's
    ``inject_framework_objects`` builds the object and sets it by name."""
    for attr, kind in _SPECIAL_PARAM_KINDS:
        name = getattr(dep, attr, None)
        if name:
            out.append(
                ParamInfo(
                    name=name,
                    kind=kind,
                    type_hint="any",
                    required=False,
                    default_value=None,
                    has_default=False,
                    model_class=None,
                    alias=None,
                    is_handler_param=True,
                    scalar_validator=None,
                )
            )


def _emit_body(dep: Any, out: list[ParamInfo]) -> None:
    """Map the handler's body params. A single ``Body`` → JSON body; ``Form``/
    ``File`` fields → form/file kinds (multipart & urlencoded both parse in Rust)."""
    body_fields = _bucket_fields(dep, "body")
    if not body_fields:
        return
    fi_names = {type(bf.field_info).__name__ for bf in body_fields}
    if fi_names <= {"Form", "File"}:
        for bf in body_fields:
            kind = "file" if type(bf.field_info).__name__ == "File" else "form"
            if kind == "form" and _is_basemodel(_unwrap_optional(bf.field_info.annotation)):
                raise Undelegable("Form model expansion → real FastAPI")
            out.append(_param_from_field(bf, kind))
    elif len(body_fields) == 1 and fi_names <= {"Body"}:
        bf = body_fields[0]
        # Body(embed=True): the wire shape is ``{"<name>": <value>}``, not the
        # bare value — real FastAPI builds a synthetic combined model for it.
        if getattr(bf.field_info, "embed", None) is True:
            raise Undelegable("Body(embed=True) → real FastAPI")
        out.append(_param_from_field(bf, "body"))
    else:
        raise Undelegable("multiple/embedded JSON body params → real FastAPI")


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

    params: list[ParamInfo] = []

    # Handler's own scalar params.
    for kind in _SCALAR_BUCKETS:
        for mf in _bucket_fields(dep, kind):
            params.append(_param_from_field(mf, kind))

    # Handler's body (single JSON body, or Form/File fields).
    _emit_body(dep, params)

    # Handler's special params (Request / Response / BackgroundTasks / SecurityScopes).
    _emit_special_params(dep, params)

    # Handler's dependencies (top-level → handler params).
    for i, sub in enumerate(dep.dependencies):
        _emit_dep(sub, params, str(i), is_handler_param=True)

    return params


def _serialize_via_field(content, field, flags):
    """Run a handler result through real FastAPI's response ``ModelField`` — the
    same ``validate`` + ``serialize`` sync core as ``serialize_response``."""
    # Pass real Response objects (StreamingResponse/FileResponse/…) straight through.
    try:
        from starlette.responses import Response as _Resp

        if isinstance(content, _Resp):
            return content
    except Exception:
        pass
    # Generators flow into a StreamingResponse, not response_model validation.
    if inspect.isgenerator(content) or inspect.isasyncgen(content):
        return content
    value, errors = field.validate(content, {}, loc=("response",))
    if errors:
        from fastapi.exceptions import ResponseValidationError

        try:
            from fastapi._compat import _normalize_errors

            errs = _normalize_errors(errors)
        except Exception:
            errs = errors
        raise ResponseValidationError(errors=errs, body=content)
    inc, exc, by_alias, eu, ed, en = flags
    return field.serialize(
        value,
        include=inc,
        exclude=exc,
        by_alias=by_alias,
        exclude_unset=eu,
        exclude_defaults=ed,
        exclude_none=en,
    )


def build_handler(route: Any):
    """Return the endpoint to register with the Rust door — wrapped to apply
    ``response_model`` filtering when the route declares one, else the endpoint
    unchanged. The wrapper preserves the endpoint's sync/async-ness."""
    endpoint = route.endpoint
    field = getattr(route, "response_field", None)
    if field is None:
        return endpoint

    flags = (
        getattr(route, "response_model_include", None),
        getattr(route, "response_model_exclude", None),
        getattr(route, "response_model_by_alias", True),
        getattr(route, "response_model_exclude_unset", False),
        getattr(route, "response_model_exclude_defaults", False),
        getattr(route, "response_model_exclude_none", False),
    )
    name = getattr(endpoint, "__name__", "endpoint")

    if inspect.iscoroutinefunction(endpoint):

        async def wrapper(**kwargs):
            result = await endpoint(**kwargs)
            return _serialize_via_field(result, field, flags)

    else:

        def wrapper(**kwargs):
            result = endpoint(**kwargs)
            return _serialize_via_field(result, field, flags)

    wrapper.__name__ = name
    return wrapper
