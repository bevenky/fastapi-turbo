"""Function signature introspection for route handlers.

Examines handler functions at startup and classifies each parameter so the
Rust core knows how to extract values from incoming requests.
"""

from __future__ import annotations

import inspect
import re
import typing
from typing import Any, get_type_hints

from fastapi_rs.dependencies import Depends
from fastapi_rs.param_functions import _ParamMarker, Body, Header

try:
    from pydantic_core import PydanticUndefined as _PydanticUndefined
except ImportError:  # pragma: no cover
    _PydanticUndefined = ...  # fallback: treat as plain Ellipsis


def _marker_is_required(marker: _ParamMarker) -> bool:
    """Return True if the marker represents a required parameter.

    FieldInfo converts ``default=...`` (Ellipsis) to ``PydanticUndefined``,
    so we must check both sentinels. A marker with a `default_factory` is
    NOT required — it supplies a value at call time. Pydantic v2
    additionally exposes `.is_required()`; prefer that when present
    since it correctly reflects Pydantic's required state even when the
    raw `.default` has been filled in by type inference.
    """
    if getattr(marker, "default_factory", None) is not None:
        return False
    if hasattr(marker, "is_required"):
        try:
            return bool(marker.is_required())
        except Exception:  # noqa: BLE001
            pass
    return marker.default is ... or marker.default is _PydanticUndefined

# Pattern to extract {param_name} from path strings like "/users/{user_id}/posts/{post_id}"
# Matches both `{name}` and `{name:convertor}` forms (FastAPI/Starlette use
# `:path` for multi-segment captures — we only care about the name here).
_PATH_PARAM_RE = re.compile(r"\{(\w+)(?::\w+)?\}")


def introspect_endpoint(endpoint, path: str) -> list[dict[str, Any]]:
    """Inspect a handler function and classify its parameters.

    Returns a list of dicts, each with keys:
        name, kind, type_hint, required, default_value, model_class, alias
    """
    sig = inspect.signature(endpoint)
    path_param_names = _extract_path_params(path)

    # Try to resolve type hints; fall back to raw annotations on failure.
    # For a class (used as a dep, e.g. `OAuth2PasswordRequestForm`) the
    # class-level annotations may be empty — in that case fetch the
    # `__init__` hints so `Annotated[str, Form()]` is still resolved.
    try:
        hints = get_type_hints(endpoint, include_extras=True)
    except Exception:
        hints = {}
    # For classes (used as a `Depends()` factory — e.g.
    # `OAuth2PasswordRequestForm`) the class-level annotations are empty.
    # Pull in `__init__`'s hints so `Annotated[str, Form()]` is visible.
    if isinstance(endpoint, type):
        try:
            init_hints = get_type_hints(endpoint.__init__, include_extras=True)
            for k, v in init_hints.items():
                hints[k] = v
        except Exception:
            pass

    params: list[dict[str, Any]] = []

    for name, param in sig.parameters.items():
        # Skip *args / **kwargs — these are catch-alls for sub-dependencies
        # (e.g., Starlette's HTTPBearer.__call__ uses **kwargs to absorb
        # extra request fields). They're never request parameters.
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        annotation = hints.get(name, param.annotation)
        # When hints stripped Annotated (some Python builds differ here),
        # fall back to the raw `param.annotation` if it still carries the
        # Annotated wrapper — that's the form that still has the
        # Form()/Query() markers we need.
        if (
            typing.get_origin(annotation) is not typing.Annotated
            and typing.get_origin(param.annotation) is typing.Annotated
        ):
            annotation = param.annotation
        # PEP 695 ``TypeAliasType`` (e.g. ``type Foo = Annotated[int, Depends(x)]``
        # or ``TypeAliasType("Foo", Annotated[int, Depends(x)], ...)``): unwrap
        # ``.__value__`` so the inner ``Annotated`` is what we introspect.
        if hasattr(annotation, "__value__") and type(annotation).__name__ == "TypeAliasType":
            annotation = annotation.__value__
        default = param.default
        model_class = None
        alias = None

        # Check for Depends marker BEFORE other markers
        dep_marker = _extract_depends_marker(annotation, default)
        if dep_marker is not None:
            # FastAPI semantic: `Depends()` (no arg) means "use the annotated
            # type as the dep callable" — useful for patterns like
            # `form: OAuth2PasswordRequestForm = Depends()`.
            dep_callable = dep_marker.dependency
            if dep_callable is None:
                # Peel ``Annotated[T, ...]`` to T — ``Depends()`` with
                # no dependency means "use the annotated type as the
                # dep callable", and T is callable; the ``Annotated``
                # wrapper is not.
                _dep_ann = annotation
                if typing.get_origin(_dep_ann) is typing.Annotated:
                    _args = typing.get_args(_dep_ann)
                    if _args:
                        _dep_ann = _args[0]
                if callable(_dep_ann) and _dep_ann is not inspect.Parameter.empty:
                    dep_callable = _dep_ann
                else:
                    raise TypeError(
                        f"Depends() used without callable on parameter {name!r} "
                        f"and annotation {annotation!r} is not callable"
                    )
            # ``Security(dep, scopes=[...])`` is a ``Depends`` subclass
            # carrying the scopes list. Preserve it for the resolver so
            # accumulated scopes can populate ``SecurityScopes``. Both
            # top-level (``_security_scopes_top``) and sub-dep paths
            # (``_security_scopes``) read this.
            _sec_scopes = list(getattr(dep_marker, "scopes", None) or [])
            # Effective scope for FA 0.120+ ``Depends(..., scope=...)`` —
            # ``"function"`` tears down right after the handler returns,
            # ``"request"`` (default) defers until after the response.
            _marker_scope = getattr(dep_marker, "scope", None)
            if _marker_scope not in ("function", "request"):
                _marker_scope = "request"
            params.append(
                {
                    "name": name,
                    "kind": "dependency",
                    "type_hint": "any",
                    "required": False,
                    "default_value": None,
                    "model_class": None,
                    "alias": None,
                    "dep_callable": dep_callable,
                    "use_cache": dep_marker.use_cache,
                    "_security_scopes_top": _sec_scopes,
                    "_sub_dep_scopes": _sec_scopes,
                    "_dep_scope": _marker_scope,
                }
            )
            continue

        # Check for Annotated[T, marker] pattern
        marker, inner_type = _extract_annotated_marker(annotation)

        # Keep the ORIGINAL annotation (pre-unwrap) so we can still feed
        # ``Annotated[T, Json, ...]`` to TypeAdapter — unwrapping drops
        # the Json marker and breaks JSON-body scalar validation.
        _original_before_unwrap = annotation

        # Always unwrap the inner type from Annotated so _get_type_name
        # resolves the base type (e.g., int) rather than the Annotated wrapper.
        # This is needed for both Annotated[int, Query()] and Annotated[int, Field(ge=0)].
        if inner_type is not annotation:
            annotation = inner_type

        # Detect Pydantic ``Json[T]`` by walking the original Annotated
        # metadata. For a Json-typed scalar param, Rust must extract a
        # SINGLE string (not a list), then Pydantic's Json validator
        # parses it into the target type.
        _is_json_scalar = False
        try:
            _annot_type = getattr(typing, "Annotated", None)
            if (
                _annot_type is not None
                and typing.get_origin(_original_before_unwrap) is _annot_type
            ):
                for _meta in typing.get_args(_original_before_unwrap)[1:]:
                    _mn = type(_meta).__name__
                    if _mn == "Json" or getattr(type(_meta), "__qualname__", "").endswith(".Json"):
                        _is_json_scalar = True
                        break
        except Exception:  # noqa: BLE001
            _is_json_scalar = False

        if marker is not None:
            # Use the inner type from Annotated for type resolution
            pass  # annotation already updated above

        # If no marker from Annotated, check if default is a marker.
        # Also accept stock FastAPI markers (``fastapi.params.Query`` etc.)
        # which don't subclass our ``_ParamMarker`` but carry the same
        # attributes (deprecated, title, description, alias, examples).
        if marker is None and isinstance(default, _ParamMarker):
            marker = default
        elif marker is None and default is not inspect.Parameter.empty:
            _fa_marker_names = {
                "Body", "Form", "File", "Header", "Cookie", "Query", "Path", "Security",
            }
            if type(default).__name__ in _fa_marker_names:
                marker = default

        if marker is not None:
            # Stock `fastapi.Form` / `fastapi.Query` etc. don't define
            # `_kind` (only our own `fastapi_rs.param_functions._ParamMarker`
            # subclasses do). Map the class name to the kind so both
            # marker families route through the same downstream logic.
            _MARKER_KIND_MAP = {
                "Body": "body",
                "Form": "form",
                "File": "file",
                "Header": "header",
                "Cookie": "cookie",
                "Query": "query",
                "Path": "path",
                "Security": "dependency",
            }
            kind = getattr(marker, "_kind", None) or _MARKER_KIND_MAP.get(
                type(marker).__name__, "query"
            )
            # Precedence: signature default > marker default > required.
            # User pattern: `x: Annotated[str | None, Header()] = None` —
            # here `Header()` has default=Ellipsis but the signature default
            # is None, so the effective default is None.
            _fa_marker_cls_names = {
                "Body", "Form", "File", "Header", "Cookie", "Query", "Path", "Security",
            }
            _default_is_marker = isinstance(default, _ParamMarker) or (
                type(default).__name__ in _fa_marker_cls_names
                and default is marker
            )
            if default is not inspect.Parameter.empty and not _default_is_marker:
                required = False
                default_val = default
                has_default_val = True
            else:
                required = _marker_is_required(marker)
                # When `default_factory` is set, call it to materialize the
                # default (an empty list for `default_factory=list` etc.).
                # Avoid storing `PydanticUndefined` which isn't JSON-safe.
                if not required:
                    df = getattr(marker, "default_factory", None)
                    if df is not None:
                        try:
                            default_val = df()
                        except Exception:  # noqa: BLE001
                            default_val = None
                    else:
                        md = marker.default
                        default_val = None if md is _PydanticUndefined else md
                else:
                    default_val = None
                has_default_val = not required
            alias = _compute_alias(name, marker)

            # Resolve the type hint from the (possibly unwrapped) annotation.
            # ``_is_body_type`` only drives body classification; for a
            # non-body kind (query, header, form, …) we still need
            # ``type_hint`` to reflect the raw type (e.g. `list_int`)
            # so the Rust router uses the proper coercion path.
            if kind == "body" and _is_body_type(annotation):
                type_hint = "model"
                try:
                    from pydantic import BaseModel as _BM
                    _is_plain_model = (
                        isinstance(annotation, type)
                        and issubclass(annotation, _BM)
                    )
                except ImportError:
                    _is_plain_model = False
                if _is_plain_model:
                    model_class = annotation
                else:
                    model_class = _make_type_adapter_proxy(annotation)
            elif kind == "body":
                # Scalar body param (`n: int = Body(...)`, `s: str = Body(...)`).
                # For non-embed scalars, build a TypeAdapter so the Rust
                # hot path can `.validate_json(bytes)` and produce a proper
                # 422 with the correct Pydantic error shape (int_parsing,
                # string_type…). For ``embed=True`` we leave model_class
                # unset so the combined body path (below) wraps this scalar
                # as a field of the generated ``Body_<endpoint>`` model.
                type_hint = _get_type_name(annotation)
                _is_embed = bool(getattr(marker, "embed", False))
                if (
                    not _is_embed
                    and annotation is not inspect.Parameter.empty
                    and annotation is not None
                ):
                    try:
                        # If the marker carries Pydantic constraints
                        # (allow_inf_nan, ge, le, min_length, …), build
                        # the TypeAdapter over ``Annotated[T, marker]``
                        # so the validator enforces them.
                        wrapped = annotation
                        if marker is not None and getattr(marker, "metadata", None):
                            from typing import Annotated as _Annotated
                            wrapped = _Annotated[annotation, marker]
                        model_class = _make_type_adapter_proxy(wrapped)
                    except Exception:  # noqa: BLE001
                        model_class = None
            else:
                type_hint = _get_type_name(annotation)
        elif name in path_param_names:
            kind = "path"
            type_hint = _get_type_name(annotation)
            required = True
            default_val = None
            has_default_val = False
        elif _is_special_injection_type(annotation):
            # `request: Request`, `bg: BackgroundTasks`, `ws: WebSocket` —
            # inject the raw framework object, not a request param.
            kind = _special_injection_kind(annotation)
            type_hint = "any"
            required = False
            default_val = None
            has_default_val = True  # we'll provide it
        elif _is_upload_file_type(annotation):
            # Bare `f: UploadFile` or `f: list[UploadFile]` — infer file upload.
            # Must be checked BEFORE `_is_body_type` since `list[UploadFile]`
            # also satisfies the "sequence → body" rule.
            kind = "file"
            type_hint = "file"
            required = default is inspect.Parameter.empty
            default_val = None if required else default
            has_default_val = not required
        elif _is_body_type(annotation):
            kind = "body"
            type_hint = "model"
            required = default is inspect.Parameter.empty
            default_val = None if required else default
            has_default_val = not required
            try:
                from pydantic import BaseModel as _BM
                _is_plain_model = isinstance(annotation, type) and issubclass(annotation, _BM)
            except ImportError:
                _is_plain_model = False
            if _is_plain_model:
                model_class = annotation
            else:
                # Non-BaseModel body (list[Item], dict, dict[str,X]) —
                # build a Pydantic TypeAdapter and expose its validator
                # via a `__pydantic_validator__` attribute so the Rust
                # body-cache path works uniformly.
                model_class = _make_type_adapter_proxy(annotation)
        else:
            kind = "query"
            type_hint = _get_type_name(annotation)
            required = default is inspect.Parameter.empty
            default_val = None if required else default
            has_default_val = not required

        # Build a Pydantic TypeAdapter for scalar params that have any
        # Pydantic-compatible constraint on them (ge/le/gt/lt/min_length/
        # max_length/regex/pattern). This gives us FastAPI-equivalent
        # validation of ``Query(ge=1, le=100)`` etc. We attach the adapter
        # to the param dict so Rust can invoke ``.validate_python`` at
        # request time and surface a proper 422 on violation.
        scalar_validator = None
        # Also handle Annotated[int, Field(ge=0)] where Field is a FieldInfo, not _ParamMarker
        _effective_marker = marker
        if _effective_marker is None and kind in ("path", "query", "header", "cookie"):
            # Check if the original annotation had a FieldInfo in the Annotated args
            orig_annotation = hints.get(name, param.annotation)
            _ann_origin = typing.get_origin(orig_annotation)
            _annotated_type = getattr(typing, "Annotated", None)
            if _annotated_type is not None and _ann_origin is _annotated_type:
                _ann_args = typing.get_args(orig_annotation)
                for _meta in _ann_args[1:]:
                    try:
                        from pydantic.fields import FieldInfo as _FieldInfo
                        if isinstance(_meta, _FieldInfo):
                            _effective_marker = _meta
                            break
                    except ImportError:
                        break
        if _effective_marker is not None and kind in ("path", "query", "header", "cookie", "form"):
            constraint_keys = (
                "gt", "ge", "lt", "le",
                "min_length", "max_length",
                "regex", "pattern",
                "multiple_of",
                "allow_inf_nan",
            )
            # Check both direct attributes (legacy) and FieldInfo metadata
            has_constraints = any(
                getattr(_effective_marker, k, None) is not None for k in constraint_keys
            ) or bool(getattr(_effective_marker, "metadata", None))
            if has_constraints:
                try:
                    from pydantic import Field as _PField, TypeAdapter as _PTypeAdapter
                    import typing as _typing
                    field_kwargs: dict = {}
                    # First try direct attributes (legacy path)
                    for k in constraint_keys:
                        v = getattr(_effective_marker, k, None)
                        if v is not None:
                            field_kwargs["pattern" if k == "regex" else k] = v
                    # Also walk FieldInfo.metadata for values not found as direct
                    # attributes (Pydantic stores ge/le/allow_inf_nan here).
                    if hasattr(_effective_marker, "metadata"):
                        for meta in _effective_marker.metadata:
                            for k in constraint_keys:
                                out_k = "pattern" if k == "regex" else k
                                if out_k in field_kwargs:
                                    continue
                                v = getattr(meta, k, None)
                                if v is not None:
                                    field_kwargs[out_k] = v
                    # Use the UNWRAPPED type (int / str / etc.)
                    base_type = annotation if annotation is not inspect.Parameter.empty else str
                    field_obj = _PField(**field_kwargs)
                    scalar_validator = _PTypeAdapter(
                        _typing.Annotated[base_type, field_obj]
                    )
                except Exception:
                    scalar_validator = None

        # Build a TypeAdapter for non-primitive scalar types (UUID, datetime,
        # date, time, timedelta, Decimal, HttpUrl, EmailStr, Enum, Literal)
        # on query/path/header/cookie params even without explicit
        # constraints. Without this Rust's coerce_str_to_py falls back to
        # returning the raw string, letting invalid `?uid=not-a-uuid` pass
        # through as 200. Mirrors FastAPI's automatic Pydantic validation.
        if scalar_validator is None and kind in ("path", "query", "header", "cookie", "form"):
            _needs_ta = _needs_scalar_validator(annotation) or _is_json_scalar
            if _needs_ta:
                try:
                    from pydantic import TypeAdapter as _PTypeAdapter
                    # For Json[T] params, use the Json-bearing annotation
                    # so Pydantic parses the JSON string into T.
                    ta_annotation = (
                        _original_before_unwrap if _is_json_scalar else annotation
                    )
                    scalar_validator = _PTypeAdapter(ta_annotation)
                except Exception:
                    scalar_validator = None
        # Force single-string extraction for Json-wrapped list/dict/set/tuple
        # scalars so the validator receives the full JSON payload.
        if _is_json_scalar and kind in ("query", "header", "cookie", "form"):
            type_hint = "str"

        # Track embed flag for body params
        embed = False
        media_type_override = None
        if marker is not None and isinstance(marker, Body):
            embed = getattr(marker, "embed", False)
            media_type_override = getattr(marker, "media_type", None)

        # Propagate example/examples/description from markers for OpenAPI
        example_val = None
        examples_val = None
        openapi_examples_val = None
        title_val = None
        description_val = None
        include_in_schema_val = True
        deprecated_val = None
        if marker is not None:
            example_val = getattr(marker, "example", None)
            examples_val = getattr(marker, "examples", None)
            openapi_examples_val = getattr(marker, "openapi_examples", None)
            title_val = getattr(marker, "title", None)
            description_val = getattr(marker, "description", None)
            include_in_schema_val = getattr(marker, "include_in_schema", True)
            deprecated_val = getattr(marker, "deprecated", None)

        # Detect Optional[T] / Union[T, None] on the original annotation so
        # the OpenAPI layer can emit `anyOf: [<T>, {type: null}]` (FastAPI's
        # exact shape for nullable parameters). After unwrapping we lose
        # this info otherwise.
        _original_annotation = hints.get(name, param.annotation)
        # ``hints`` is collected with ``include_extras=True`` so Annotated
        # wrappers are preserved; peel to the inner type before the
        # Union check.
        if typing.get_origin(_original_annotation) is typing.Annotated:
            _inner_args = typing.get_args(_original_annotation)
            if _inner_args:
                _original_annotation = _inner_args[0]
        _is_optional_param = False
        if typing.get_origin(_original_annotation) is typing.Union:
            _is_optional_param = any(
                a is type(None) for a in typing.get_args(_original_annotation)
            )
        # Capture enum values for Enum-typed params (incl. Optional[Enum],
        # `Literal["a", "b"]`) so the parameter schema includes the
        # `enum:` list.
        _enum_values = None
        try:
            import enum as _enum_mod
            probe = annotation
            if typing.get_origin(probe) is typing.Union:
                probe = next(
                    (a for a in typing.get_args(probe) if a is not type(None)),
                    probe,
                )
            if isinstance(probe, type) and issubclass(probe, _enum_mod.Enum):
                _enum_values = [m.value for m in probe]
            elif typing.get_origin(probe) is typing.Literal:
                _enum_values = list(typing.get_args(probe))
        except Exception:  # noqa: BLE001
            pass

        # Pull numeric / length / pattern constraints off the marker (or
        # the FieldInfo in an Annotated[...]) so the OpenAPI layer can emit
        # `minimum` / `maximum` / `minLength` / `pattern` in the parameter
        # schema. Mirrors what FastAPI does when it extracts the Field
        # metadata while building the spec.
        constraint_out: dict[str, Any] = {}
        if _effective_marker is not None:
            for ck in ("gt", "ge", "lt", "le", "min_length", "max_length", "regex", "pattern", "multiple_of"):
                v = getattr(_effective_marker, ck, None)
                if v is None and hasattr(_effective_marker, "metadata"):
                    for meta in _effective_marker.metadata:
                        mv = getattr(meta, ck, None)
                        if mv is not None:
                            v = mv
                            break
                if v is not None:
                    constraint_out[ck] = v

        params.append(
            {
                "name": name,
                "kind": kind,
                "type_hint": type_hint,
                "required": required,
                "default_value": default_val,
                "has_default": has_default_val,
                "model_class": model_class,
                "alias": alias,
                "_embed": embed,
                "media_type": media_type_override,
                "example": example_val,
                "examples": examples_val,
                "openapi_examples": openapi_examples_val,
                "title": title_val,
                "description": description_val,
                "include_in_schema": include_in_schema_val,
                "deprecated": deprecated_val,
                "scalar_validator": scalar_validator,
                "enum_class": _probe_enum_class(annotation),
                # Python collection to wrap the extracted list in before
                # calling the handler — ``set`` / ``frozenset`` /
                # ``tuple`` / None (= plain list).
                "container_type": _get_container_type(annotation),
                "_is_optional": _is_optional_param,
                "_enum_values": _enum_values,
                "_unwrapped_annotation": _unwrap_optional(annotation),
                # Preserve the ORIGINAL marker (`Form()`, `Query()`, etc.)
                # and the full annotation so downstream schema builders
                # can hand them back to Pydantic verbatim. Prefer the
                # signature's raw annotation (which retains the
                # Annotated wrapper) over the potentially-stripped
                # `hints` entry.
                "_raw_marker": marker,
                "_raw_annotation": (
                    param.annotation
                    if typing.get_origin(param.annotation) is typing.Annotated
                    else hints.get(name, param.annotation)
                ),
                **constraint_out,
            }
        )

    # Post-processing: expand parameter-model annotations (FA 0.115+)
    # BEFORE running multi-body handling so the synthesized model-
    # builder step is visible to the rest of the pipeline.
    params = _maybe_expand_param_models(params)

    # Post-processing: handle multiple body params and Body(embed=True)
    params = _maybe_embed_body_params(params, endpoint)

    return params


_PARAM_MODEL_MISSING = object()  # sentinel for field not supplied in request


def _maybe_expand_param_models(params: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """FastAPI 0.115+ query/cookie/header parameter-model expansion.

    When a handler declares ``p: Annotated[MyModel, Query()]`` where
    ``MyModel`` is a Pydantic ``BaseModel``, FA flattens each field of
    the model into its own query param. Calling
    ``GET /x?p=a&p=b&size=20`` then hydrates ``MyModel(p=["a", "b"],
    size=20)`` and hands the instance to the handler.

    We emulate this at introspection time: replace the single
    model-typed param with N synthetic extraction params (one per
    field) plus a synthetic dep-like "builder" step that reconstructs
    the model and passes it under the original variable name.
    """
    try:
        from pydantic import BaseModel as _BM
        from pydantic.fields import FieldInfo as _FI
    except ImportError:
        return params

    out: list[dict[str, Any]] = []
    for p in params:
        kind = p.get("kind")
        if kind not in ("query", "cookie", "header", "form"):
            out.append(p)
            continue
        ann = p.get("_unwrapped_annotation")
        model_cls = None
        if isinstance(ann, type) and issubclass(ann, _BM):
            model_cls = ann
        if model_cls is None:
            out.append(p)
            continue

        # Expand: for each model field, synthesize an extraction param.
        handler_var = p["name"]
        # (field_name, resolved_key, wire_alias_for_builder)
        field_inputs: list[tuple[str, str, str]] = []
        # Header param-models inherit FA's convert_underscores default
        # (True on ``Header(...)``). Pull it off the raw marker when
        # present.
        _marker = p.get("_raw_marker")
        _convert_underscores = getattr(_marker, "convert_underscores", True)
        for field_name, field_info in model_cls.model_fields.items():
            # Prefer validation_alias → alias → field name (FA order).
            # Track the EXTRACTION-side wire name separately from the
            # BUILDER-side key we feed Pydantic: for headers with
            # ``convert_underscores=True`` (the FA default) we extract
            # under the hyphenated header name but hand the value back
            # to the model by its Python field name.
            val_alias = field_info.validation_alias
            builder_key = field_name
            if isinstance(val_alias, str):
                wire_alias = val_alias
                builder_key = val_alias
            elif field_info.alias:
                wire_alias = field_info.alias
                builder_key = field_info.alias
            else:
                wire_alias = field_name
                if kind == "header" and _convert_underscores and "_" in wire_alias:
                    wire_alias = wire_alias.replace("_", "-")

            # Use a non-underscore-prefixed synthesized name so it's
            # safe to hand into ``pydantic.create_model`` when the
            # form-embedding pass runs after us (Pydantic forbids
            # leading underscores on field names).
            resolved_key = f"pm_{handler_var}__{field_name}"
            field_ann = field_info.annotation

            # For schema emission we still want the RICH type hint so
            # OpenAPI shows `type: array` / `type: integer` / enum
            # values. But for RUNTIME extraction we want the raw wire
            # value (string / list of strings) so Pydantic can run its
            # full validator on the whole model at once — that gives
            # FA-shaped errors (``loc=["query","f"]``, ``input={...other
            # fields}``) on missing-required and on wrong-type.
            schema_type_hint = _get_type_name(field_ann)
            sub_container = _get_container_type(field_ann)
            # Extraction type: plain ``list_str`` if the annotation is
            # sequence-shaped (so we collect all repeated query
            # occurrences), else ``str``. Pydantic coerces downstream.
            if schema_type_hint.startswith("list_"):
                runtime_type_hint = "list_str"
            else:
                runtime_type_hint = "str"
            sub_is_optional = False
            try:
                _origin = typing.get_origin(field_ann)
                if _origin is typing.Union:
                    sub_is_optional = any(
                        a is type(None) for a in typing.get_args(field_ann)
                    )
            except Exception:  # noqa: BLE001
                pass

            # Mark the field as optional at extraction time so missing
            # values don't short-circuit before the model builder
            # runs — Pydantic emits the "missing" error with full
            # dict context (matches FA).
            sub_param = {
                "name": resolved_key,
                "kind": kind,
                "type_hint": runtime_type_hint,
                "required": False,
                "default_value": _PARAM_MODEL_MISSING,
                "has_default": True,
                "model_class": None,
                "alias": wire_alias,
                "_embed": False,
                "media_type": None,
                "example": None,
                "examples": None,
                "title": field_info.title,
                "description": field_info.description,
                "include_in_schema": p.get("include_in_schema", True),
                "deprecated": None,
                "scalar_validator": None,
                "enum_class": None,
                "container_type": None,
                "_is_optional": sub_is_optional,
                "_enum_values": (
                    list(typing.get_args(field_ann))
                    if typing.get_origin(field_ann) is typing.Literal
                    else None
                ),
                "_unwrapped_annotation": _unwrap_optional(field_ann),
                "_raw_marker": None,
                "_raw_annotation": field_ann,
                "_is_handler_param": False,
                # OpenAPI uses this to emit the field name + rich type.
                "_param_model_field_name": field_name,
                "_param_model_owner": handler_var,
                "_param_model_class": model_cls,
                "_param_model_schema_type_hint": schema_type_hint,
                "_param_model_container_type": sub_container,
                "_param_model_field_info": field_info,
                "_param_model_field_ann": field_ann,
            }
            # Constraint values for the OpenAPI param-schema builder.
            for meta in getattr(field_info, "metadata", []) or []:
                for ck in ("gt", "ge", "lt", "le", "min_length",
                           "max_length", "pattern", "multiple_of"):
                    v = getattr(meta, ck, None)
                    if v is not None:
                        sub_param[ck] = v
            out.append(sub_param)
            field_inputs.append((field_name, resolved_key, builder_key))

        # Synthetic builder dep: takes the extracted fields and
        # constructs the BaseModel, passing it under the original
        # handler-variable name. Pydantic validates the whole model
        # (so missing-required / wrong-type errors surface uniformly).
        def _make_builder(model_cls, fields, loc_prefix, source_kind):
            # Map from synthesized extraction-kwarg name → wire alias that
            # the model expects. Pydantic validates by ``alias`` /
            # ``validation_alias``; handing it the field Python name would
            # miss the alias and produce a spurious "missing" error.
            key_to_alias = {resolved: alias for _fn, resolved, alias in fields}
            # Fields whose annotation is a list/set/tuple/dict AND have a
            # container default (``= []`` / ``= {}``) — FA always
            # reports these in the validation ``input`` dict even when
            # empty, because header/query multi-value extraction yields
            # the empty default when no matching value is present. Fields
            # without a default stay missing so Pydantic emits the
            # "missing" 422 error.
            from pydantic_core import PydanticUndefined as _Und
            empty_container_defaults: dict = {}
            for _fn, _resolved, _alias in fields:
                fi = model_cls.model_fields.get(_fn)
                if fi is None:
                    continue
                ann = fi.annotation
                origin = typing.get_origin(ann)
                try:
                    dv = fi.get_default(call_default_factory=True)
                except Exception:
                    dv = _Und
                if dv is _Und or dv is None:
                    continue
                import builtins as _bi
                if origin in (list, _bi.list, set, _bi.set, frozenset, tuple, _bi.tuple):
                    empty_container_defaults[_alias] = list(dv) if hasattr(dv, "__iter__") else []
                elif origin in (dict, _bi.dict):
                    empty_container_defaults[_alias] = dict(dv) if hasattr(dv, "items") else {}
                else:
                    # Non-None scalar default (``str = "nothing"``, ``int = 0``,
                    # …). FA surfaces these in the validation ``input`` dict
                    # when the request omits the field — matches test
                    # ``test_forms_single_model::test_no_data``.
                    empty_container_defaults[_alias] = dv
            raw_kw_by_source = {
                "query": "__fastapi_rs_raw_query__",
                "header": "__fastapi_rs_raw_headers__",
                "cookie": "__fastapi_rs_raw_cookies__",
                "form": "__fastapi_rs_raw_form__",
            }

            # Set of wire aliases we KNOW map to fields — used to strip
            # them out of the raw dict before merging (otherwise
            # ``save-data`` ends up as an "extra" alongside the properly
            # extracted ``save_data``). Only the BUILDER alias goes in
            # here, NOT the python field name — FA keeps raw-only keys
            # (e.g. user sending ``?p=x`` when the field uses an alias)
            # visible in the validation ``input`` dict.
            wire_aliases_known = {alias for _fn, _r, alias in fields}
            # Also include hyphen-converted versions for headers so
            # ``save-data`` is stripped when ``save_data`` is the field.
            if source_kind == "header":
                wire_aliases_known |= {
                    fn.replace("_", "-") for fn in wire_aliases_known
                }

            def _build(__raw__=None, **extracted):
                raw_dict = __raw__
                supplied: dict = {}
                if isinstance(raw_dict, dict):
                    for k, v in raw_dict.items():
                        if k in wire_aliases_known:
                            continue
                        supplied[k] = v
                for alias, default_val in empty_container_defaults.items():
                    supplied.setdefault(alias, default_val)
                for k, v in extracted.items():
                    if v is _PARAM_MODEL_MISSING:
                        continue
                    alias = key_to_alias.get(k, k)
                    supplied[alias] = v
                try:
                    return model_cls.model_validate(supplied)
                except Exception as exc:
                    raise _param_model_build_error(exc, loc_prefix)
            _build.__name__ = f"_build_{model_cls.__name__}"
            _build._fastapi_rs_raw_source = raw_kw_by_source.get(source_kind)
            return _build

        # FA uses "body" as the loc prefix for form-model errors
        # (forms are classified as body in the error shape).
        loc_prefix = "body" if kind == "form" else kind
        # Synthesize the builder
        builder_func = _make_builder(model_cls, field_inputs, loc_prefix, kind)
        # Normal dep_input_map is 2-tuples (dep_param_name, source_key).
        # The builder's resolved_key is both its param name AND its
        # source — the extraction step writes under the same key.
        # Also wire the raw-request-dict kwarg (``__raw__``) to the
        # Rust-populated slot so the builder can feed it to
        # ``model_validate`` — gives FA-shaped error ``input``.
        dep_input_map_2 = [(resolved, resolved) for _fn, resolved, _a in field_inputs]
        _raw_kw = getattr(builder_func, "_fastapi_rs_raw_source", None)
        if _raw_kw:
            dep_input_map_2.append(("__raw__", _raw_kw))
        builder_step = {
            "name": handler_var,
            "kind": "dependency",
            "type_hint": "any",
            "required": p.get("required", True),
            "default_value": None,
            "has_default": False,
            "model_class": None,
            "alias": None,
            "dep_callable": builder_func,
            "_original_dep_callable": builder_func,
            "dep_callable_id": id(builder_func),
            "is_async_dep": False,
            "is_generator_dep": False,
            "dep_input_map": dep_input_map_2,
            "use_cache": False,
            "_is_handler_param": True,
            "_is_param_model_builder": True,
            "_param_model_class": model_cls,
            "_param_model_loc_prefix": loc_prefix,
            "include_in_schema": False,
            "enum_class": None,
            "container_type": None,
            "scalar_validator": None,
        }
        out.append(builder_step)

    return out


def _param_model_build_error(exc, loc_prefix: str):
    """Convert a Pydantic ValidationError from a parameter-model
    builder into an ``HTTPException(422)`` whose detail list prepends
    the query/cookie/header ``loc`` segment. Falls back to re-raising
    the original exception if it isn't a ValidationError.
    """
    try:
        from pydantic import ValidationError
        from fastapi_rs.exceptions import HTTPException as _HE
        if isinstance(exc, ValidationError):
            detail = []
            for err in exc.errors(include_url=False):
                loc = (loc_prefix,) + tuple(err.get("loc", ()))
                item: dict = {
                    "type": err.get("type"),
                    "loc": list(loc),
                    "msg": err.get("msg"),
                }
                inp = err.get("input")
                item["input"] = inp if inp is not None else None
                ctx = err.get("ctx")
                if ctx is not None:
                    ctx_out: dict = {}
                    for k, v in ctx.items():
                        if isinstance(v, Exception):
                            ctx_out[k] = {}
                        else:
                            ctx_out[k] = v
                    if ctx_out:
                        item["ctx"] = ctx_out
                detail.append(item)
            return _HE(status_code=422, detail=detail)
    except Exception:  # noqa: BLE001
        pass
    return exc


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _extract_path_params(path: str) -> set[str]:
    """Return the set of parameter names found in a path template."""
    return set(_PATH_PARAM_RE.findall(path))


def _extract_depends_marker(annotation, default) -> Depends | None:
    """Check if a parameter has a Depends marker, either via default or Annotated."""
    if isinstance(default, Depends):
        return default

    origin = typing.get_origin(annotation)
    if origin is None:
        return None

    annotated_type = getattr(typing, "Annotated", None)
    if annotated_type is None:
        try:
            from typing_extensions import Annotated as annotated_type
        except ImportError:
            return None

    if origin is annotated_type:
        args = typing.get_args(annotation)
        for meta in args[1:]:
            if isinstance(meta, Depends):
                return meta

    return None


def _extract_annotated_marker(annotation) -> tuple[_ParamMarker | None, Any]:
    """If annotation is Annotated[T, marker], return (marker, T). Else (None, annotation).

    Python 3.14 changes:
      - `Annotated[T, ...].__origin__` returns T (the inner type), not
        Annotated. Use `typing.get_origin()` which returns Annotated.
      - `Annotated.__args__` holds only (T,); metadata lives in
        `__metadata__`. Use `typing.get_args()` which returns (T, *meta)
        for backward-compatible access.
    """
    origin = typing.get_origin(annotation)
    if origin is None:
        return None, annotation

    annotated_type = getattr(typing, "Annotated", None)
    if annotated_type is None:
        try:
            from typing_extensions import Annotated as annotated_type  # type: ignore[assignment]
        except ImportError:
            return None, annotation

    if origin is annotated_type:
        args = typing.get_args(annotation)
        if len(args) >= 2:
            inner_type = args[0]
            # Accept stock FastAPI's markers (`fastapi.Body`, `fastapi.Form`,
            # etc.) as well as our own. Both inherit from Pydantic's
            # FieldInfo. The compat shim replaces `fastapi.*` at
            # sys.modules level but apps that import `from fastapi.security
            # import OAuth2PasswordRequestForm` still receive stock
            # markers — duck-type on class name so either kind works.
            _fa_marker_names = {
                "Body", "Form", "File", "Header", "Cookie", "Query", "Path", "Security",
            }
            # Collect ALL markers in the Annotated metadata. FastAPI supports
            # multiple markers with the same kind (``Annotated[int, Query(gt=2),
            # Query(lt=10)]``) — their constraints must be merged so the
            # generated TypeAdapter enforces BOTH bounds.
            found_markers: list = []
            for meta in args[1:]:
                if isinstance(meta, _ParamMarker):
                    found_markers.append(meta)
                    continue
                cls_name = type(meta).__name__
                if cls_name in _fa_marker_names:
                    found_markers.append(meta)
            if not found_markers:
                return None, inner_type
            if len(found_markers) == 1:
                return found_markers[0], inner_type
            # Multiple markers: merge their metadata/constraints. Use
            # Pydantic's ``merge_field_infos`` which keeps only explicitly
            # set attributes and lets later ones override earlier ones.
            try:
                from pydantic.fields import FieldInfo as _FI
                merged = _FI.merge_field_infos(*found_markers)
                # Preserve the first marker's subclass identity (Query/Header/…)
                # and our custom attributes (``_kind``, ``include_in_schema``,
                # ``convert_underscores``, ``media_type``, ``embed``, ``example``,
                # ``openapi_examples``, ``regex``, ``pattern``). These aren't
                # FieldInfo-native so ``merge_field_infos`` drops them.
                first = found_markers[0]
                try:
                    merged.__class__ = type(first)
                except TypeError:
                    pass
                for _attr in (
                    "_kind", "include_in_schema", "convert_underscores",
                    "media_type", "embed", "example", "openapi_examples",
                    "regex", "pattern",
                ):
                    for m in found_markers:
                        if hasattr(m, _attr):
                            try:
                                setattr(merged, _attr, getattr(m, _attr))
                            except Exception:  # noqa: BLE001
                                pass
                return merged, inner_type  # type: ignore[return-value]
            except Exception:  # noqa: BLE001
                return found_markers[0], inner_type
        return None, args[0] if args else annotation

    return None, annotation


def _compute_alias(name: str, marker: _ParamMarker) -> str | None:
    """Compute the lookup alias for a parameter.

    Matches FastAPI's ``get_validation_alias`` + ``field.alias`` rule:
    ``validation_alias or alias or name``. If neither is set, Header with
    convert_underscores=True falls back to the dash-converted name.
    """
    va = getattr(marker, "validation_alias", None)
    if isinstance(va, str) and va:
        return va
    if marker.alias is not None:
        return marker.alias

    if isinstance(marker, Header) and getattr(marker, "convert_underscores", True):
        converted = name.replace("_", "-")
        if converted != name:
            return converted

    return None


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
        if origin is typing.Union:
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


def _get_container_type(annotation) -> str | None:
    """Return ``"set"`` / ``"frozenset"`` / ``"tuple"`` when the
    annotation is a sequence-like collection whose Python container
    differs from ``list``. Used by the query/header extractor to wrap
    the element list in the right type before the handler sees it.

    ``list`` and plain scalars return None so the hot path doesn't pay
    a wrap step.
    """
    if annotation is inspect.Parameter.empty or annotation is None:
        return None
    origin = typing.get_origin(annotation) or getattr(annotation, "__origin__", None)
    if origin is typing.Union:
        for a in typing.get_args(annotation):
            if a is type(None):
                continue
            return _get_container_type(a)
    if origin is set:
        return "set"
    if origin is frozenset:
        return "frozenset"
    if origin is tuple:
        return "tuple"
    return None


def _is_upload_file_type(annotation) -> bool:
    """True if annotation is UploadFile, list[UploadFile], Optional[UploadFile], etc."""
    if annotation is inspect.Parameter.empty or annotation is None:
        return False
    try:
        from fastapi_rs.param_functions import UploadFile as _UF
    except ImportError:
        return False
    # Direct annotation: `f: UploadFile`
    if annotation is _UF:
        return True
    # Generic: `f: list[UploadFile]`, `f: Optional[UploadFile]`, etc.
    origin = getattr(annotation, "__origin__", None)
    if origin is not None:
        args = getattr(annotation, "__args__", ())
        for arg in args:
            if arg is _UF:
                return True
    return False


def _is_special_injection_type(annotation) -> bool:
    """Framework-provided objects that FastAPI auto-injects based on type:

    * ``Request`` / ``HTTPConnection`` — the incoming request
    * ``WebSocket`` — the WebSocket wrapper
    * ``Response`` — the outgoing response shell (user can mutate headers)
    * ``BackgroundTasks`` — a BackgroundTasks holder
    * ``SecurityScopes`` — the scopes declared by enclosing ``Security(...)`` calls
    """
    if annotation is inspect.Parameter.empty or annotation is None:
        return False
    try:
        from fastapi_rs.requests import Request, HTTPConnection
        from fastapi_rs.websockets import WebSocket
        from fastapi_rs.background import BackgroundTasks
        from fastapi_rs.responses import Response as _Response
        from fastapi_rs.security import SecurityScopes
    except ImportError:
        return False
    if not isinstance(annotation, type):
        return False
    return issubclass(annotation, (
        Request, HTTPConnection, WebSocket, BackgroundTasks, _Response, SecurityScopes,
    ))


def _special_injection_kind(annotation) -> str:
    """Return a short string tag the router uses to dispatch injection."""
    from fastapi_rs.requests import Request, HTTPConnection
    from fastapi_rs.websockets import WebSocket
    from fastapi_rs.background import BackgroundTasks
    from fastapi_rs.responses import Response as _Response
    from fastapi_rs.security import SecurityScopes
    if isinstance(annotation, type):
        if issubclass(annotation, BackgroundTasks):
            return "inject_background_tasks"
        if issubclass(annotation, WebSocket):
            return "inject_websocket"
        if issubclass(annotation, _Response):
            return "inject_response"
        if issubclass(annotation, SecurityScopes):
            return "inject_security_scopes"
        if issubclass(annotation, (Request, HTTPConnection)):
            return "inject_request"
    return "query"


_PRIMITIVE_SCALARS = (int, float, str, bool, bytes)


def _unwrap_optional(annotation):
    """If annotation is `Optional[X]` / `Union[X, None]` return X, else
    return the annotation unchanged."""
    if annotation is inspect.Parameter.empty or annotation is None:
        return None
    if typing.get_origin(annotation) is typing.Union:
        non_none = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return annotation


def _probe_enum_class(annotation):
    """Return the Enum subclass referenced by `annotation`, unwrapping
    `Optional[X]` / `Union[X, None]` first. Returns None if not an Enum.
    """
    if annotation is inspect.Parameter.empty or annotation is None:
        return None
    import enum as _enum_mod
    probe = annotation
    if typing.get_origin(probe) is typing.Union:
        probe = next(
            (a for a in typing.get_args(probe) if a is not type(None)),
            probe,
        )
    try:
        if isinstance(probe, type) and issubclass(probe, _enum_mod.Enum):
            return probe
    except Exception:  # noqa: BLE001
        pass
    return None


def _needs_scalar_validator(annotation) -> bool:
    """True if the annotation is a non-primitive scalar that Rust's built-in
    coerce (int/float/bool/str) can't validate — Pydantic TypeAdapter must
    run to distinguish `?uid=abc` from a well-formed UUID.
    """
    if annotation is inspect.Parameter.empty or annotation is None:
        return False
    # Pydantic ``Json[T]`` expands to ``Annotated[T, <Json marker>]``.
    # The marker class is ``pydantic.types.Json`` (not the plain class
    # ``pydantic.Json``). Detect either by walking the annotation
    # metadata — Json types must go through TypeAdapter since Rust's
    # raw-string coerce won't parse the JSON payload.
    try:
        from pydantic import Json as _PJson
        if annotation is _PJson:
            return True
        if typing.get_origin(annotation) is getattr(typing, "Annotated", None):
            args = typing.get_args(annotation)
            for meta in args[1:]:
                mt = type(meta)
                if (
                    meta is _PJson
                    or mt.__name__ == "Json"
                    or getattr(mt, "__qualname__", "").endswith(".Json")
                ):
                    return True
    except ImportError:
        pass
    origin = typing.get_origin(annotation)
    # Unwrap Optional[T] / Union[T, None]
    if origin is typing.Union:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if not args:
            return False
        return _needs_scalar_validator(args[0])
    # list[T] / tuple[T, ...] are handled by Rust's list_ path; don't wrap.
    import collections.abc as _cabc
    if origin in (list, tuple, set, frozenset, _cabc.Sequence, _cabc.MutableSequence, _cabc.Set):
        return False
    # Literal[...] is a non-primitive scalar — must validate.
    if origin is typing.Literal or (hasattr(typing, "LiteralString") and origin is getattr(typing, "LiteralString", None)):
        return True
    if isinstance(annotation, type):
        if annotation in _PRIMITIVE_SCALARS:
            return False
        import enum as _enum_mod
        if issubclass(annotation, _enum_mod.Enum):
            return True
        # UUID / datetime / date / time / timedelta / Decimal / Path / etc.
        # — anything else that isn't primitive.
        return True
    return False


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


def _make_type_adapter_proxy(annotation):
    return _TypeAdapterProxy(annotation)


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

    __slots__ = ("_validator", "_is_model")

    def __init__(self, annotation):
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

    def validate_json(self, body):
        import json as _json

        if isinstance(body, (bytes, bytearray, memoryview)):
            raw = bytes(body)
        elif isinstance(body, str):
            raw = body.encode("utf-8")
        else:
            return self._validator.validate_python(body, from_attributes=True)
        try:
            data = _json.loads(raw)
        except _json.JSONDecodeError as e:
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
        return self._validator.validate_python(data, from_attributes=True)

    def validate_python(self, value):
        return self._validator.validate_python(value, from_attributes=True)


def _make_fa_body_validator(annotation):
    # If a ``_TypeAdapterProxy`` was handed in (set by the body-type
    # resolver for ``list[Item]`` / ``dict[str, X]`` / scalar body),
    # unwrap it back to the raw annotation so the FA-style adapter is
    # built on the real type.
    if isinstance(annotation, _TypeAdapterProxy):
        annotation = annotation._annotation
    try:
        return _FABodyValidator(annotation)
    except Exception:  # noqa: BLE001
        return None


def _is_body_type(annotation) -> bool:
    """Return True if the annotation must be read from the request body.

    FastAPI's rule: scalars and sequences-of-scalars default to query; anything
    richer (a BaseModel, a list/tuple of models, a dict, a generic mapping)
    defaults to body unless the user annotated otherwise. This matters for
    SQLAlchemy bulk endpoints (`payload: list[ItemIn]`) and Redis Lua helpers
    (`body: dict`) that otherwise end up classified as query params.
    """
    if annotation is inspect.Parameter.empty or annotation is None:
        return False

    # Unwrap ``Optional[T]`` / ``Union[T, None]`` / ``T | None`` before
    # classifying — ``foo: Foo | None = None`` is a body param in FA.
    origin = typing.get_origin(annotation)
    if origin is typing.Union:
        _non_none = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(_non_none) == 1:
            return _is_body_type(_non_none[0])
        # Multi-member Union: body if any member is body-typed.
        return any(_is_body_type(a) for a in _non_none)

    try:
        from pydantic import BaseModel

        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return True
    except ImportError:
        pass

    # Python ``@dataclass`` classes — FA treats them as body params
    # (goes through a Pydantic TypeAdapter under the hood).
    try:
        import dataclasses as _dc

        if isinstance(annotation, type) and _dc.is_dataclass(annotation):
            return True
    except Exception:  # noqa: BLE001
        pass

    # Bare `dict` / `Dict` always goes to body — arbitrary key/value can't
    # round-trip through a query string.
    if annotation is dict:
        return True

    args = typing.get_args(annotation)

    # dict[...]  /  Dict[...]  /  Mapping[...]
    try:
        import collections.abc as _cabc
        _mapping_origins = (dict, _cabc.Mapping, _cabc.MutableMapping)
    except Exception:  # noqa: BLE001
        _mapping_origins = (dict,)
    if origin in _mapping_origins:
        return True

    # FastAPI's rule: ANY sequence annotation (`list[str]`, `tuple[...]`,
    # `set[int]`, …) with no explicit marker goes to the body, not to
    # query. The only exception is a bare class like `list` without
    # args — we leave that as query so single-element handlers keep
    # working. Matches `field_annotation_is_complex` in
    # `fastapi/_compat/shared.py`.
    try:
        import collections.abc as _cabc
        _seq_origins = (list, tuple, set, frozenset, _cabc.Sequence, _cabc.MutableSequence, _cabc.Set)
    except Exception:  # noqa: BLE001
        _seq_origins = (list, tuple, set, frozenset)
    if origin in _seq_origins:
        return True

    return False


def _maybe_embed_body_params(params: list[dict[str, Any]], endpoint) -> list[dict[str, Any]]:
    """Handle multiple body parameters and Body(embed=True).

    When there are multiple body params, or any body param uses embed=True,
    the expected JSON body wraps each param under its name:
        {"item": {...}, "user": {...}}

    We create a wrapper Pydantic model that represents this combined body,
    and replace all individual body params with a single body param. The
    handler is then wrapped to unpack the combined model into the original
    params.
    """
    body_params = [p for p in params if p["kind"] == "body"]
    if not body_params:
        return params

    # Check if any body param has embed=True
    has_embed = any(p.get("_embed") for p in body_params)

    # If single body param without embed, no transformation needed
    if len(body_params) == 1 and not has_embed:
        return params

    # Need to embed: create a wrapper model. For plain typed body params
    # we preserve numeric/length/pattern constraints via a Pydantic Field
    # so the generated schema retains them (FastAPI does the same).
    from pydantic import create_model, Field as _PField

    def _build_field_info(bp):
        kwargs = {}
        for ck in ("gt", "ge", "lt", "le", "min_length", "max_length",
                   "pattern", "regex", "multiple_of"):
            v = bp.get(ck)
            if v is not None:
                kwargs["pattern" if ck == "regex" else ck] = v
        if bp.get("description"):
            kwargs["description"] = bp["description"]
        if bp.get("title"):
            kwargs["title"] = bp["title"]
        # FA: ``Body(embed=True, alias="p_alias")`` means Pydantic
        # should look up the JSON key ``"p_alias"`` for this field.
        # Without this, Pydantic accepts either name and the test
        # ``test_required_alias_by_name`` (which expects 422 when the
        # client sends ``{"p": "hello"}`` but the alias is
        # ``"p_alias"``) fails. The ``alias`` on our bp dict already
        # carries validation_alias → alias fallback from ``_compute_alias``.
        raw_marker = bp.get("_raw_marker")
        if raw_marker is not None:
            if getattr(raw_marker, "alias", None) is not None:
                kwargs["alias"] = raw_marker.alias
            va = getattr(raw_marker, "validation_alias", None)
            if isinstance(va, str) and va:
                kwargs["validation_alias"] = va
        if not bp.get("required", True):
            kwargs["default"] = bp.get("default_value")
        else:
            kwargs["default"] = ...
        return _PField(**kwargs) if len(kwargs) > 1 or "default" not in kwargs else kwargs.get("default", ...)

    field_definitions = {}
    for bp in body_params:
        model_cls = bp.get("model_class")
        # `_TypeAdapterProxy` is a runtime shim, not a usable schema type
        # for `create_model`. Unwrap back to the raw annotation
        # (e.g. `dict`, `list[Item]`) so Pydantic can build the field.
        if isinstance(model_cls, _TypeAdapterProxy):
            model_cls = model_cls._annotation
        if model_cls is not None:
            # If Body(alias=...)/validation_alias is set on a
            # non-scalar (list/dict/Optional-wrapped) body param, route
            # it through ``_build_field_info`` too so the alias lands
            # on the generated Pydantic field. Without this, list body
            # params silently ignore the alias and the embed'd field is
            # accepted only by its Python name (failing FA parity).
            raw_marker = bp.get("_raw_marker")
            has_alias_kw = raw_marker is not None and (
                getattr(raw_marker, "alias", None) is not None
                or (
                    isinstance(getattr(raw_marker, "validation_alias", None), str)
                    and raw_marker.validation_alias
                )
            )
            if has_alias_kw:
                field_definitions[bp["name"]] = (model_cls, _build_field_info(bp))
            elif bp["required"]:
                field_definitions[bp["name"]] = (model_cls, ...)
            else:
                field_definitions[bp["name"]] = (model_cls, bp["default_value"])
        else:
            # Non-model body params (plain types with Body marker) —
            # preserve constraints via Pydantic Field metadata. Enum-
            # typed params use the Enum class so the emitted schema gets
            # the proper `enum: [...]` list.
            type_map = {"int": int, "float": float, "bool": bool, "str": str}
            enum_cls = bp.get("enum_class")
            if enum_cls is not None:
                py_type = enum_cls
            else:
                py_type = type_map.get(bp["type_hint"], Any)
            # Wrap in Optional[...] when the body param is typed as
            # ``T | None`` / ``Optional[T]`` — Pydantic then emits
            # ``anyOf: [{<T>}, {type: null}]`` which is FA's exact
            # shape for nullable embedded body fields.
            if bp.get("_is_optional"):
                from typing import Optional as _Optional
                py_type = _Optional[py_type]
            field_obj = _build_field_info(bp)
            field_definitions[bp["name"]] = (py_type, field_obj)

    # FastAPI auto-names the combined-body model as `Body_<endpoint_name>`.
    # Using the same name lets OpenAPI consumers match schemas by canonical
    # ref (`#/components/schemas/Body_login_form` etc.) across both servers.
    _endpoint_name = getattr(endpoint, "__name__", "endpoint")
    CombinedBody = create_model(f"Body_{_endpoint_name}", **field_definitions)

    # Build the new params list: keep non-body params, replace body params
    # with a single combined body param named "_combined_body".
    # When every embedded body param is optional the whole combined body
    # is optional too — FA accepts an empty body and uses defaults.
    _combined_required = any(bp.get("required", True) for bp in body_params)
    # FA honours an explicit ``Body(media_type=...)`` even across combined
    # body params — take the first non-default one so ``application/vnd.api+json``
    # on both fields still reaches the emitted ``content`` key.
    _combined_media_type = None
    for bp in body_params:
        mt = bp.get("media_type")
        if mt:
            _combined_media_type = mt
            break
    new_params = [p for p in params if p["kind"] != "body"]
    new_params.append({
        "name": "_combined_body",
        "kind": "body",
        "type_hint": "model",
        "required": _combined_required,
        "default_value": None,
        "has_default": not _combined_required,
        "model_class": CombinedBody,
        "alias": None,
        "_embed": False,
        "media_type": _combined_media_type,
        "_body_param_names": [bp["name"] for bp in body_params],
        # The unwrap wrapper needs this to appear in handler_param_names so
        # the filtered kwargs include `_combined_body`, which the wrapper
        # then splits back into the individual body param names.
        "_is_handler_param": True,
    })

    return new_params
