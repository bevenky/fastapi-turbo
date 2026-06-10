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

Coverage maps the hot-path surface onto the Rust ``kind`` vocabulary the door
implements:

* scalar path/query/header/cookie params (constraints included — the validator is
  real FastAPI's own ``ModelField._type_adapter``);
* a single Pydantic/scalar JSON body, ``Body(embed=True)``, and multiple JSON
  bodies (validated against real FastAPI's combined ``route.body_field`` then
  scattered to handler params by getter deps);
* ``Form`` / ``File`` multipart & urlencoded bodies, incl. ``Form`` model-expansion;
* query/header/cookie parameter-models (``Annotated[Model, Query()]``) — expanded
  per-field with a synthetic builder dependency;
* special params — ``Request``/``HTTPConnection`` → ``inject_request``,
  ``Response`` → ``inject_response``, ``BackgroundTasks`` →
  ``inject_background_tasks``, ``SecurityScopes`` → ``inject_security_scopes``;
* ``response_model`` filtering — :func:`build_handler` wraps the endpoint so the
  result is run through real FastAPI's own ``ModelField.validate``/``.serialize``;
* dependencies — sync, async, nested, cached, and sync ``yield`` (entered on the
  door with teardown after the response); deps that themselves take a Request /
  Response / BackgroundTasks / SecurityScopes (incl. security schemes).

The remaining delegation to real FastAPI is the narrow tail that needs the
event-loop exit stack: **async-generator** ``yield`` dependencies. Those raise
:class:`Undelegable` and the caller falls back to real FastAPI.
``kind`` strings are the SHORT form the Rust extractor matches.
"""

from __future__ import annotations

import dataclasses
import inspect
import typing
from collections.abc import Mapping as _abc_Mapping
from typing import Any

from fastapi_turbo._fastapi_turbo_core import ParamInfo
from fastapi_turbo._door_support import _TypeAdapterProxy, _get_type_name

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


def _callable_is_async(fn: Any) -> bool:
    """True for an ``async def`` OR a SYNC ``functools.wraps`` wrapper over an
    ``async def`` (``inspect.iscoroutinefunction`` is False for the latter, but the
    call returns a coroutine the door must await). Follows ``__wrapped__``."""
    if inspect.iscoroutinefunction(fn):
        return True
    try:
        return inspect.iscoroutinefunction(inspect.unwrap(fn))
    except Exception:
        return False


def _has_json_marker(fi: Any) -> bool:
    """A pydantic ``Json[...]`` field carries a Json marker in field_info.metadata.
    FastAPI never getlists a Json field — it reads the single raw JSON string and
    lets the Json TypeAdapter decode it."""
    for m in getattr(fi, "metadata", None) or ():
        tn = type(m).__name__
        if tn == "Json" or getattr(type(m), "__qualname__", "").endswith(".Json"):
            return True
    return False


def _is_union_of_basemodels(ann: Any) -> bool:
    """True for ``ModelA | ModelB`` (a Union whose non-None members are all
    BaseModels) — a single Form field of this shape validates as a union model."""
    origin = typing.get_origin(ann)
    if origin is typing.Union or (origin is not None and getattr(origin, "__name__", "") == "UnionType"):
        members = [a for a in typing.get_args(ann) if a is not type(None)]
        return len(members) >= 2 and all(_is_basemodel(m) for m in members)
    return False


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
    # Unwrap Optional[Model] (``item: Item | None``) so the body still validates
    # through the model (defaults included) instead of being passed as a raw dict.
    body_ann = _unwrap_optional(ann)
    is_body_model = kind == "body" and _is_basemodel(body_ann)
    # A @dataclass body validates via a TypeAdapter proxy (the door's model_class
    # path) just like a BaseModel — _is_basemodel is False for dataclasses.
    is_dataclass_body = (
        kind == "body"
        and not is_body_model
        and isinstance(body_ann, type)
        and dataclasses.is_dataclass(body_ann)
    )
    explicit_alias = _alias_of(fi)
    if name is not None:
        resolved_name = name
        alias = explicit_alias or mf.name  # carry the real lookup key
        is_handler_param = False
    else:
        resolved_name = mf.name
        alias = explicit_alias
    if is_body_model or is_dataclass_body:
        type_hint = "model"
    else:
        type_hint = _get_type_name(ann)
        # A pydantic Json[...] scalar (query/header/cookie/form): force single-value
        # so the door's scalar branch runs the Json TypeAdapter (decodes the JSON
        # string) instead of treating list[...] as repeated values.
        if kind in ("query", "header", "cookie", "form") and _has_json_marker(fi):
            type_hint = "str"
        # A TYPED dict body (``dict[int, float]``) needs structural validation (the
        # door validates "dict" via the field TypeAdapter); a BARE ``dict`` stays
        # "str" → the lenient body_json path (keeps strict_content_type lax handling).
        elif kind == "body" and typing.get_origin(body_ann) in (dict, _abc_Mapping):
            if typing.get_args(body_ann):
                type_hint = "dict"
    return ParamInfo(
        name=resolved_name,
        kind=kind,
        type_hint=type_hint,
        required=required,
        default_value=_default_of(fi),
        has_default=not required,
        model_class=(
            body_ann
            if is_body_model
            else (_TypeAdapterProxy(body_ann) if is_dataclass_body else None)
        ),
        alias=alias,
        scalar_validator=_field_validator(mf, is_body_model, kind),
        is_handler_param=is_handler_param,
    )


# Sentinel for "this expanded field was absent from the request" — the model
# builder drops it so Pydantic applies the field's own default / missing logic.
# Identity survives the Rust round-trip (clone_ref keeps the same object).
class _PMMissing:
    __slots__ = ()


_PM_MISSING = _PMMissing()


def _http_422_from_validation(exc, loc_prefix: str):
    """Wrap a Pydantic ValidationError as ``HTTPException(422)`` whose detail
    prepends the source ``loc`` segment — the door maps status_code → response,
    so this surfaces as a 422 just like real FastAPI's request validation."""
    from fastapi import HTTPException

    detail = []
    for e in exc.errors(include_url=False):
        detail.append(
            {
                "type": e.get("type"),
                "loc": [loc_prefix, *tuple(e.get("loc", ()))],
                "msg": e.get("msg"),
                "input": e.get("input"),
            }
        )
    return HTTPException(status_code=422, detail=detail)


def _make_model_builder(model_cls, loc_prefix: str):
    """Synthetic dependency: rebuild a parameter-/form-model from its extracted
    fields. Absent fields (``_PM_MISSING``) are dropped so model defaults apply."""
    from pydantic import ValidationError

    def _build(**fields):
        supplied = {k: v for k, v in fields.items() if v is not _PM_MISSING}
        try:
            return model_cls.model_validate(supplied)
        except ValidationError as exc:
            raise _http_422_from_validation(exc, loc_prefix) from None

    _build.__name__ = f"_build_{getattr(model_cls, '__name__', 'model')}"
    return _build


def _make_request_param_model_builder(mf: Any, kind: str):
    """Synthetic dependency for a query/header/cookie PARAMETER-MODEL
    (``f: Annotated[Model, Query()/Header()/Cookie()]``): validate it EXACTLY like
    real FastAPI by calling its own ``request_params_to_args`` on the live request
    multidict. That single call reproduces alias / validation_alias precedence,
    header underscore→dash, list (repeated-value) fields, ``model_config`` extra
    (forbid/allow/ignore), and the loc-prefixed 422 — so all the alias/extra/
    constraint variants match upstream byte-for-byte. Raises ``HTTPException(422)``
    (which the door renders) on validation errors."""
    # REAL FastAPI's helper — the compat shim replaces ``fastapi.dependencies.utils``
    # with a stub, so reach it via the pre-shim ``_real_fastapi`` capture.
    from fastapi_turbo.applications import _real_fastapi

    request_params_to_args = _real_fastapi.dependencies.utils.request_params_to_args

    def _build(request):
        if kind == "query":
            received = request.query_params
        elif kind == "header":
            received = request.headers
        else:  # cookie
            received = request.cookies
        values, errors = request_params_to_args([mf], received)
        if errors:
            from fastapi import HTTPException
            from fastapi.encoders import jsonable_encoder

            raise HTTPException(status_code=422, detail=jsonable_encoder(errors))
        return values[mf.name]

    _build.__name__ = f"_pm_{getattr(mf, 'name', 'model')}"
    return _build


def _emit_request_param_model(
    mf: Any, kind: str, out: list[ParamInfo], uid: str, result_key: str, *, is_handler_param: bool
) -> str:
    """Emit a query/header/cookie parameter-model as a synthetic builder dependency
    fed the injected ``Request`` — it validates via real FastAPI's
    ``request_params_to_args`` (see ``_make_request_param_model_builder``). Returns
    ``result_key`` (holds the validated model instance)."""
    req_src = f"_pmreq{uid}"
    out.append(
        ParamInfo(
            name=req_src,
            kind="inject_request",
            type_hint="any",
            required=False,
            default_value=None,
            has_default=False,
            model_class=None,
            alias=None,
            is_handler_param=False,
            scalar_validator=None,
        )
    )
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
            dep_callable=_make_request_param_model_builder(mf, kind),
            dep_callable_id=None,
            is_async_dep=False,
            is_generator_dep=False,
            dep_input_names=[("request", req_src)],
            is_handler_param=is_handler_param,
            scalar_validator=None,
        )
    )
    return result_key


def _make_form_param_model_builder(bf: Any):
    """Synthetic dependency for a single FORM parameter-model
    (``f: Annotated[Model, Form()]``): validate it EXACTLY like real FastAPI by
    awaiting the request form and calling its own ``request_body_to_args`` — which
    reproduces alias/validation_alias precedence, repeated-value lists,
    ``model_config`` extra (forbid/allow/ignore), and the ``("body", field)``-prefixed
    422. Async (form parsing awaits); raises ``HTTPException(422)`` on errors."""
    from fastapi_turbo.applications import _real_fastapi

    request_body_to_args = _real_fastapi.dependencies.utils.request_body_to_args

    async def _build(request):
        form = await request.form()
        # Single form param-model → not embedded (request_body_to_args extracts the
        # model's fields from the FormData and validates the whole thing).
        values, errors = await request_body_to_args([bf], form, embed_body_fields=False)
        if errors:
            from fastapi import HTTPException
            from fastapi.encoders import jsonable_encoder

            raise HTTPException(status_code=422, detail=jsonable_encoder(errors))
        return values[bf.name]

    _build.__name__ = f"_fm_{getattr(bf, 'name', 'model')}"
    return _build


def _emit_form_param_model(
    bf: Any, out: list[ParamInfo], result_key: str, *, is_handler_param: bool
) -> str:
    """Emit a single FORM parameter-model as an ASYNC builder dependency fed the
    injected ``Request`` (it awaits ``request.form()``). The ``inject_request`` also
    makes the door read the request body so the form is available."""
    req_src = f"_fmreq_{bf.name}"
    out.append(
        ParamInfo(
            name=req_src,
            kind="inject_request",
            type_hint="any",
            required=False,
            default_value=None,
            has_default=False,
            model_class=None,
            alias=None,
            is_handler_param=False,
            scalar_validator=None,
        )
    )
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
            dep_callable=_make_form_param_model_builder(bf),
            dep_callable_id=None,
            is_async_dep=True,
            is_generator_dep=False,
            dep_input_names=[("request", req_src)],
            is_handler_param=is_handler_param,
            scalar_validator=None,
        )
    )
    return result_key


def _make_getter(attr: str):
    """Synthetic dependency: pull one field off a validated combined-body model."""

    def _get(cb):
        return getattr(cb, attr)

    _get.__name__ = f"_get_{attr}"
    return _get


def _expand_param_model(
    model_cls, kind: str, out: list[ParamInfo], uid: str, result_key: str, *, is_handler_param: bool
) -> str:
    """Expand a query/header/cookie/form parameter-MODEL into one extraction param
    per model field plus a synthetic builder dependency that reconstructs it — the
    same shape real FastAPI flattens these into, mapped onto the door's deps."""
    convert_underscores = kind == "header"
    loc_prefix = "body" if kind == "form" else kind
    wiring: list[tuple[str, str]] = []
    for fname, finfo in model_cls.model_fields.items():
        alias = finfo.validation_alias or finfo.alias or fname
        if not isinstance(alias, str):
            alias = fname
        wire = alias
        if kind == "header" and convert_underscores and alias == fname and "_" in wire:
            wire = wire.replace("_", "-")
        src_key = f"_pm{uid}__{fname}"
        out.append(
            ParamInfo(
                name=src_key,
                kind=kind,
                type_hint=_get_type_name(finfo.annotation),
                required=False,
                default_value=_PM_MISSING,
                has_default=True,
                model_class=None,
                alias=wire,
                scalar_validator=None,
                is_handler_param=False,
            )
        )
        wiring.append((alias, src_key))
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
            dep_callable=_make_model_builder(model_cls, loc_prefix),
            dep_callable_id=None,
            is_async_dep=False,
            is_generator_dep=False,
            dep_input_names=wiring,
            is_handler_param=is_handler_param,
            scalar_validator=None,
        )
    )
    return result_key


def _emit_combined_body(route: Any, dep: Any, out: list[ParamInfo]) -> None:
    """Embed / multiple JSON bodies: validate the whole body against real FastAPI's
    own combined ``route.body_field`` model (extracted as a hidden ``_combined_body``
    input), then scatter each handler body param off it via a getter dependency."""
    bf = getattr(route, "body_field", None)
    combined_model = bf.field_info.annotation if bf is not None else None
    if combined_model is None or not _is_basemodel(combined_model):
        raise Undelegable("no combined body_field → real FastAPI")
    # The combined body is OPTIONAL when its field_info has a default (e.g.
    # ``Body(embed=True) = None`` → every embedded field optional) — an absent body
    # then builds the model from defaults instead of 422-ing. Required for a normal
    # embed / multiple-body.
    try:
        body_required = bool(bf.field_info.is_required())
    except Exception:  # noqa: BLE001
        body_required = True
    out.append(
        ParamInfo(
            name="_combined_body",
            kind="body",
            type_hint="model",
            required=body_required,
            default_value=None,
            has_default=not body_required,
            model_class=combined_model,
            alias=None,
            scalar_validator=None,
            is_handler_param=False,
        )
    )
    for mf in _bucket_fields(dep, "body"):
        out.append(
            ParamInfo(
                name=mf.name,
                kind="dependency",
                type_hint="any",
                required=True,
                default_value=None,
                has_default=False,
                model_class=None,
                alias=None,
                dep_callable=_make_getter(mf.name),
                dep_callable_id=None,
                is_async_dep=False,
                is_generator_dep=False,
                dep_input_names=[("cb", "_combined_body")],
                is_handler_param=True,
                scalar_validator=None,
            )
        )


def _scopes_for(dep: Any, kind: str) -> list[str]:
    """OAuth2 scopes for an ``inject_security_scopes`` param: the dependant's
    accumulated ``oauth_scopes`` (``parent + own`` down the ``Security(...,
    scopes=[...])`` chain, order-preserving). Empty for every other inject kind —
    the door builds ``SecurityScopes(scopes=...)`` from this."""
    if kind != "inject_security_scopes":
        return []
    return list(getattr(dep, "oauth_scopes", None) or [])


_U64_MASK = (1 << 64) - 1


def _dep_cache_id(dep: Any, call: Any) -> int | None:
    """Per-request dependency cache key as a ``u64`` for the door's ``dep_cache``.

    Mirrors FastAPI's ``Dependant.cache_key`` — ``(call, scopes, scope)`` — so a
    dependency reached via different ``Security(..., scopes=[...])`` chains is
    cached SEPARATELY (it observes different ``SecurityScopes``), while a plain
    dependency that doesn't use scopes stays shared. Returns ``None`` when
    ``use_cache`` is False (no caching), matching the prior behavior. For the
    common no-scope case the value is exactly ``id(call)`` (zero blast radius)."""
    if not getattr(dep, "use_cache", True):
        return None
    try:
        scopes = dep.cache_key[1]  # () unless the dependant actually uses scopes
    except Exception:
        scopes = ()
    # FA 0.136 cache_key = (call, scopes, scope): a function-scope dep is a DISTINCT
    # cache entry from the same callable at request scope, so they're separate
    # instances (e.g. one yield-dep used at both scopes must not be deduped).
    scope = getattr(dep, "scope", None) or "request"
    if not scopes and scope == "request":
        return id(call)  # common case — zero blast radius
    return (id(call) ^ (hash((tuple(scopes), scope)) & _U64_MASK)) & _U64_MASK


def _emit_dep_special_params(dep: Any, out: list[ParamInfo], uid: str) -> list[tuple[str, str]]:
    """A dependency that takes Request/Response/BackgroundTasks/SecurityScopes:
    emit each as an ``inject_*`` dep-input (the door builds it into ``resolved``)
    and return the wiring so the dep receives it by its own param name."""
    wiring: list[tuple[str, str]] = []
    for attr, kind in _SPECIAL_PARAM_KINDS:
        pname = getattr(dep, attr, None)
        if pname:
            src_key = f"_dep{uid}__{pname}"
            out.append(
                ParamInfo(
                    name=src_key,
                    kind=kind,
                    type_hint="any",
                    required=False,
                    default_value=None,
                    has_default=False,
                    model_class=None,
                    alias=None,
                    is_handler_param=False,
                    scalar_validator=None,
                    oauth_scopes=_scopes_for(dep, kind),
                )
            )
            wiring.append((pname, src_key))
    return wiring


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
                    oauth_scopes=_scopes_for(dep, kind),
                )
            )


def _emit_body(route: Any, dep: Any, out: list[ParamInfo]) -> None:
    """Map the handler's body params. A single ``Body`` → JSON body; ``Form``/
    ``File`` fields → form/file kinds (a ``Form`` model expands per-field); embed /
    multiple JSON bodies → real FastAPI's combined body model + per-field getters."""
    body_fields = _bucket_fields(dep, "body")
    if not body_fields:
        return
    fi_names = {type(bf.field_info).__name__ for bf in body_fields}
    if fi_names <= {"Form", "File"}:
        # A single Form parameter-model → validate via real FastAPI's own
        # request_body_to_args (exact alias/extra/list parity, like query/header/
        # cookie). The field-by-field _expand_param_model couldn't reproduce
        # model_config extra or the raw-dict 422 ``input``.
        if len(body_fields) == 1 and fi_names == {"Form"}:
            _form_ann = body_fields[0].field_info.annotation
            if _is_basemodel(_unwrap_optional(_form_ann)) or _is_union_of_basemodels(_form_ann):
                _emit_form_param_model(
                    body_fields[0], out, body_fields[0].name, is_handler_param=True
                )
                return
        for bf in body_fields:
            kind = "file" if type(bf.field_info).__name__ == "File" else "form"
            ann = _unwrap_optional(bf.field_info.annotation)
            if kind == "form" and _is_basemodel(ann):
                _expand_param_model(
                    ann, "form", out, f"_fm_{bf.name}", bf.name, is_handler_param=True
                )
            else:
                out.append(_param_from_field(bf, kind))
    elif len(body_fields) == 1 and fi_names <= {"Body"}:
        bf = body_fields[0]
        # Body(embed=True): wire shape is {"<name>": value}, not the bare value —
        # real FastAPI builds a synthetic combined model, so go through it.
        if getattr(bf.field_info, "embed", None) is True:
            _emit_combined_body(route, dep, out)
        else:
            out.append(_param_from_field(bf, "body"))
    else:
        # multiple JSON bodies — combined model + getters.
        _emit_combined_body(route, dep, out)


def _override_aware_dep(original: Any, app: Any, is_async: bool):
    """Wrap a dependency callable so ``app.dependency_overrides`` is honored at
    REQUEST time (the adapter bakes the original callable at build time; the clone
    path resolved overrides per request). The door calls this wrapper with the
    original dep's wired inputs, so a SAME-signature override (the documented
    pattern) works transparently; the wrapper is keyed for dedup by the ORIGINAL
    callable's id (set on the ParamInfo, not this wrapper)."""
    if is_async:

        async def _aw(*args, **kwargs):
            ov = app.dependency_overrides
            eff = ov.get(original, original) if ov else original
            res = eff(*args, **kwargs)
            if inspect.isawaitable(res):
                return await res
            return res

        return _aw

    def _w(*args, **kwargs):
        ov = app.dependency_overrides
        eff = ov.get(original, original) if ov else original
        return eff(*args, **kwargs)

    return _w


def _async_gen_sync_bridge(original: Any, app: Any):
    """Present an ASYNC-generator yield-dependency to the door as a plain SYNC
    generator, driving its ``__anext__``/``athrow``/``aclose`` on the shared worker
    loop (``_async_worker.submit`` releases the GIL while waiting → no deadlock).
    The door's existing sync-gen enter (``send(None)``) and teardown
    (``send``/``throw``/``close``) machinery then handles it IDENTICALLY to a sync
    yield-dep — same enter, same teardown, same error/exit-stack semantics.

    CONTEXTVARS: the dep runs on the worker loop (a different thread), but FastAPI
    runs a yield-dep's set()/yield/reset() in the SAME contextvar context as the
    handler. So we run __anext__/athrow/aclose inside a captured ``Context`` and
    propagate the dep's contextvar mutations onto the request thread for the
    handler to see (then restore on teardown so they don't leak to the next
    request). ``app.dependency_overrides`` are resolved at call time."""
    import contextvars

    def _g(**kwargs):
        from fastapi_turbo import _async_worker

        eff = original
        if app is not None:
            ov = app.dependency_overrides
            if ov:
                eff = ov.get(original, original)
        agen = eff(**kwargs)
        if not hasattr(agen, "__anext__"):
            # An override swapped in a sync generator / plain callable.
            if hasattr(agen, "send"):
                yield from agen
            else:
                yield agen
            return

        # One context for the whole dep lifecycle (enter + teardown), so a
        # token created before yield is valid for reset() after yield.
        ctx = contextvars.copy_context()
        before = contextvars.copy_context()
        restore_tokens: list = []

        def _propagate():
            # Apply the contextvar changes the dep made (in ctx) onto THIS thread's
            # context so the handler sees them; remember tokens to undo on teardown.
            for var in ctx:
                new = ctx[var]
                if var not in before or before[var] is not new:
                    restore_tokens.append((var, var.set(new)))

        def _restore():
            for var, tok in reversed(restore_tokens):
                try:
                    var.reset(tok)
                except (ValueError, LookupError):
                    pass
            restore_tokens.clear()

        try:
            value = _async_worker.submit(agen.__anext__(), context=ctx)
        except StopAsyncIteration:
            return
        _propagate()
        try:
            yield value
        except GeneratorExit:
            # Door close() (a later dep/extraction failed) — run the async gen's
            # cleanup on the loop, then honor GeneratorExit.
            try:
                _async_worker.submit(agen.aclose(), context=ctx)
            except BaseException:  # noqa: BLE001 — teardown error, response done
                pass
            _restore()
            raise
        except BaseException as exc:  # noqa: BLE001
            # Door throw(exc) (handler raised) — throw it into the async gen. A
            # converted/re-raised error propagates (submit surfaces it here); a
            # clean finish (StopAsyncIteration) means the dep SWALLOWED it → signal
            # the door via StopIteration so it raises FastAPIError, matching sync.
            try:
                _async_worker.submit(agen.athrow(exc), context=ctx)
            except StopAsyncIteration:
                _restore()
                return
            except BaseException:
                _restore()
                raise
            _restore()
            raise
        else:
            # Door send(None) (success) — advance past the yield; a well-formed dep
            # finishes with StopAsyncIteration. Any error here propagates and the
            # door swallows it on the success path (same as a sync yield-dep).
            try:
                _async_worker.submit(agen.__anext__(), context=ctx)
            except StopAsyncIteration:
                pass
            finally:
                _restore()

    return _g


def _emit_dep(
    dep: Any, out: list[ParamInfo], uid: str, *, is_handler_param: bool, app: Any = None
) -> str:
    """Emit the flat params for one dependency (post-order: its own inputs +
    sub-deps first, then the ``"dependency"`` entry). Returns the ``resolved`` key
    holding the dep's result, so a parent can wire it as an input."""
    call = dep.call
    # Use FastAPI's own callable classification (handles callable instances with an
    # async/gen ``__call__``, e.g. security schemes). Sync ``yield`` deps run on the
    # door directly; ASYNC ``yield`` deps are bridged to a sync generator that
    # drives __anext__/athrow/aclose on the worker loop (see _async_gen_sync_bridge)
    # so the door's sync-gen enter/teardown machinery handles them uniformly.
    is_async_gen = dep.is_async_gen_callable
    is_gen = dep.is_gen_callable or is_async_gen
    if _bucket_fields(dep, "body"):
        raise Undelegable("dependency with a body param → real FastAPI")
    if getattr(dep, "websocket_param_name", None) is not None:
        raise Undelegable("dependency uses websocket_param_name → real FastAPI")

    # 0. special params the dep takes (Request/Response/BackgroundTasks/Scopes).
    input_wiring: list[tuple[str, str]] = _emit_dep_special_params(dep, out, uid)

    # 1. the dependency's own scalar params — extracted, fed to the dep by name.
    for kind in _SCALAR_BUCKETS:
        for mf in _bucket_fields(dep, kind):
            src_key = f"_dep{uid}__{mf.name}"
            ann = _unwrap_optional(mf.field_info.annotation)
            # A query/header/cookie parameter-model input to this dep → validate via
            # real FastAPI's request_params_to_args (same as the handler path).
            if kind in ("query", "header", "cookie") and _is_basemodel(ann):
                _emit_request_param_model(
                    mf, kind, out, f"{uid}_{mf.name}", src_key, is_handler_param=False
                )
            else:
                out.append(_param_from_field(mf, kind, name=src_key))
            input_wiring.append((mf.name, src_key))

    # 2. sub-dependencies first (so they resolve before this dep), wired by the
    #    parent param name the sub-dep result feeds.
    for k, sub in enumerate(dep.dependencies):
        sub_key = _emit_dep(sub, out, f"{uid}_{k}", is_handler_param=False, app=app)
        input_wiring.append((sub.name, sub_key))

    # 3. the dependency itself. Wrap the callable so runtime app.dependency_overrides
    #    are honored (dedup id stays the ORIGINAL callable's). An async-generator
    #    dep is wrapped in the sync-gen bridge instead (it resolves overrides too).
    result_key = dep.name if (is_handler_param and dep.name) else f"_dep{uid}"
    if is_async_gen:
        dep_callable = _async_gen_sync_bridge(call, app)
    elif app is not None:
        dep_callable = _override_aware_dep(call, app, dep.is_coroutine_callable)
    else:
        dep_callable = call
    out.append(
        ParamInfo(
            name=result_key,
            kind="dependency",
            type_hint="any",
            required=True,
            default_value=None,
            model_class=None,
            has_default=False,
            alias=None,
            dep_callable=dep_callable,
            dep_callable_id=_dep_cache_id(dep, call),
            is_async_dep=dep.is_coroutine_callable,
            is_generator_dep=is_gen,
            dep_input_names=input_wiring,
            is_handler_param=is_handler_param,
            scalar_validator=None,
            # FastAPI 0.136 ``Depends(..., scope="function")`` — function-scope yield
            # deps tear down before the response (raise → response); default/request
            # scope tears down after the body.
            is_function_scope=(getattr(dep, "scope", None) == "function"),
        )
    )
    return result_key


def extract_params_from_route(route: Any, app: Any = None) -> list[ParamInfo]:
    """Map a real FastAPI ``APIRoute``'s ``route.dependant`` to the Rust engine's
    flat ``ParamInfo`` list. Raises :class:`Undelegable` for surface the Rust door
    does not cover, so the caller falls back to real FastAPI. ``app`` (when given)
    makes dependency callables honor runtime ``app.dependency_overrides``."""
    dep = route.dependant

    params: list[ParamInfo] = []

    # Handler's own scalar params — a BaseModel-typed one is a parameter-model
    # (FastAPI 0.115+), expanded per-field + a builder dep.
    for kind in _SCALAR_BUCKETS:
        for idx, mf in enumerate(_bucket_fields(dep, kind)):
            ann = _unwrap_optional(mf.field_info.annotation)
            if _is_basemodel(ann):
                # query/header/cookie parameter-model → validate via real FastAPI's
                # request_params_to_args (exact alias/extra/list parity). Form keeps
                # the field-by-field expansion (the builder can't await request.form()).
                if kind in ("query", "header", "cookie"):
                    _emit_request_param_model(
                        mf, kind, params, f"_{kind}{idx}_{mf.name}", mf.name, is_handler_param=True
                    )
                else:
                    _expand_param_model(
                        ann, kind, params, f"_{kind}{idx}_{mf.name}", mf.name, is_handler_param=True
                    )
            else:
                params.append(_param_from_field(mf, kind))

    # Handler's body (single JSON body, Form/File, or embed/multiple bodies).
    _emit_body(route, dep, params)

    # Handler's special params (Request / Response / BackgroundTasks / SecurityScopes).
    _emit_special_params(dep, params)

    # Handler's dependencies. Parameter deps (``d = Depends(...)``) have a name and
    # their result is passed to the handler; route/router/global deps
    # (``dependencies=[...]``) have name=None and run for their side effects only
    # (auth checks etc.) without being passed to the handler.
    for i, sub in enumerate(dep.dependencies):
        _emit_dep(sub, params, str(i), is_handler_param=(sub.name is not None), app=app)

    return params


def _serialize_via_field(content, field, flags, endpoint_ctx=None):
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
        from fastapi_turbo.exceptions import (
            _DoorResponseValidationError as ResponseValidationError,
        )

        try:
            from fastapi._compat import _normalize_errors

            errs = _normalize_errors(errors)
        except Exception:
            errs = errors
        # endpoint_ctx (function/file/line/path) lets a user
        # @app.exception_handler(ResponseValidationError) log which handler
        # produced the bad response — parity with the dispatcher path.
        raise ResponseValidationError(
            errors=errs, body=content, endpoint_ctx=endpoint_ctx
        )
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


def endpoint_ctx_for(endpoint, path=None) -> dict:
    """Build the ``endpoint_ctx`` dict (function/file/line/path) that
    Request/ResponseValidationError carry so user exception handlers can log
    which handler produced the error — and so ``str(exc)`` renders ``in
    <function>`` (which needs function AND file AND line). Mirrors the
    dispatcher's ``_endpoint_ctx``."""
    ctx = {"function": getattr(endpoint, "__name__", "endpoint")}
    try:
        ctx["file"] = inspect.getsourcefile(endpoint)
        ctx["line"] = inspect.getsourcelines(endpoint)[1]
    except (TypeError, OSError):
        pass
    if path is not None:
        ctx["path"] = path
    return ctx


def _route_has_uploads(route: Any) -> bool:
    """True when the handler takes an ``UploadFile`` (``File()`` body field or a
    ``UploadFile``/``list[UploadFile]`` annotation). Such routes need the door to
    close the upload after the response — Starlette/FastAPI parity
    (``test_upload_file_is_closed``). Deps with body params are declined, so only
    the handler's own body params can carry uploads."""
    try:
        from starlette.datastructures import UploadFile as _UF

        for bf in getattr(route.dependant, "body_params", []) or []:
            if type(bf.field_info).__name__ == "File":
                return True
            ann = _unwrap_optional(bf.field_info.annotation)
            if ann is _UF:
                return True
            args = getattr(ann, "__args__", None)
            if args and any(a is _UF for a in args):
                return True
    except Exception:
        return False
    return False


def build_handler(route: Any, *, ctx_path: str | None = None):
    """Return the endpoint to register with the Rust door — wrapped to apply
    ``response_model`` filtering when the route declares one, and to render the
    result through a CUSTOM ``response_class`` when the route sets one (so those
    routes ride the fast adapter path). The wrapper preserves the endpoint's
    sync/async-ness. Default-JSON routes with no response_model return the
    endpoint unchanged — the door does the fast Rust dict→JSON render.

    ``ctx_path`` overrides ``route.path`` in the error context — the P10.4
    reuse path hands in the decoration-built route, whose ``path`` lacks any
    include/router prefix; the caller passes the rd's full path instead."""
    endpoint = route.endpoint
    field = getattr(route, "response_field", None)

    # Custom (non-default) response_class → render the result through it. A custom
    # class is an actual TYPE (``response_class=HTMLResponse``, or a router default
    # the route-dict already resolved to a class); the framework DEFAULT is a
    # ``DefaultPlaceholder`` *instance* (not a type) → left to the door's fast Rust
    # dict→JSON render. ``isinstance(rc, type)`` avoids comparing against
    # shim-rebound DefaultPlaceholder/JSONResponse classes.
    rc = getattr(route, "response_class", None)
    custom_rc = rc if isinstance(rc, type) else None

    has_uploads = _route_has_uploads(route)

    if field is None and custom_rc is None and not has_uploads:
        if not inspect.iscoroutinefunction(endpoint) and _callable_is_async(endpoint):
            # A sync ``@wraps`` wrapper over an ``async def``: return a true
            # coroutine function so the door awaits it (iscoroutinefunction(endpoint)
            # is False, so returning it unchanged would render an un-awaited
            # coroutine). Genuinely-async endpoints are returned as-is — the
            # adapter's is_async gate already drives them on the loop.
            async def _await_wrapper(**kwargs):
                return await endpoint(**kwargs)

            _await_wrapper.__name__ = getattr(endpoint, "__name__", "endpoint")
            return _await_wrapper
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
    _ctx = endpoint_ctx_for(endpoint, ctx_path or getattr(route, "path", None))

    # Route-level status_code (real APIRoute carries it) — bake it into a custom
    # response_class render, since a returned Response keeps its own status (the
    # door's default-status hook only applies to non-Response results).
    _status_code = getattr(route, "status_code", None)

    def _finish(result):
        if field is not None:
            result = _serialize_via_field(result, field, flags, _ctx)
        if custom_rc is not None:
            from starlette.responses import Response as _Resp

            # Already a Response (StreamingResponse / explicit return) → pass through;
            # the door merges any dep-injected Response onto it.
            if isinstance(result, _Resp):
                return result
            if _status_code is not None:
                return custom_rc(result, status_code=_status_code)
            return custom_rc(result)
        return result

    # Close UploadFile(s) handed to the handler after the response is built —
    # Starlette parity (``test_upload_file_is_closed``). Imported lazily to keep
    # the adapter import light. Runs in ``finally`` so uploads close whether the
    # handler succeeds or raises (matches Starlette's request-scoped teardown).
    if has_uploads:
        from fastapi_turbo._route_helpers import _close_upload_files

        if _callable_is_async(endpoint):

            async def wrapper(**kwargs):
                try:
                    return _finish(await endpoint(**kwargs))
                finally:
                    _close_upload_files(kwargs)

        else:

            def wrapper(**kwargs):
                try:
                    return _finish(endpoint(**kwargs))
                finally:
                    _close_upload_files(kwargs)

    elif _callable_is_async(endpoint):
        # async def, OR a sync ``@wraps`` wrapper over an async def — await the
        # returned coroutine so a true coroutine-function handler reaches the door
        # (else the door would call it sync and render an un-awaited coroutine).
        async def wrapper(**kwargs):
            return _finish(await endpoint(**kwargs))

    else:

        def wrapper(**kwargs):
            return _finish(endpoint(**kwargs))

    wrapper.__name__ = name
    return wrapper
