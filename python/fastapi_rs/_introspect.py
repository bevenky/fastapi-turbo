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
    so we must check both sentinels.
    """
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

    # Try to resolve type hints; fall back to raw annotations on failure
    try:
        hints = get_type_hints(endpoint, include_extras=True)
    except Exception:
        hints = {}

    params: list[dict[str, Any]] = []

    for name, param in sig.parameters.items():
        # Skip *args / **kwargs — these are catch-alls for sub-dependencies
        # (e.g., Starlette's HTTPBearer.__call__ uses **kwargs to absorb
        # extra request fields). They're never request parameters.
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        annotation = hints.get(name, param.annotation)
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
                if callable(annotation) and annotation is not inspect.Parameter.empty:
                    dep_callable = annotation
                else:
                    raise TypeError(
                        f"Depends() used without callable on parameter {name!r} "
                        f"and annotation {annotation!r} is not callable"
                    )
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
                }
            )
            continue

        # Check for Annotated[T, marker] pattern
        marker, inner_type = _extract_annotated_marker(annotation)

        # Always unwrap the inner type from Annotated so _get_type_name
        # resolves the base type (e.g., int) rather than the Annotated wrapper.
        # This is needed for both Annotated[int, Query()] and Annotated[int, Field(ge=0)].
        if inner_type is not annotation:
            annotation = inner_type

        if marker is not None:
            # Use the inner type from Annotated for type resolution
            pass  # annotation already updated above

        # If no marker from Annotated, check if default is a marker
        if marker is None and isinstance(default, _ParamMarker):
            marker = default

        if marker is not None:
            kind = marker._kind
            # Precedence: signature default > marker default > required.
            # User pattern: `x: Annotated[str | None, Header()] = None` —
            # here `Header()` has default=Ellipsis but the signature default
            # is None, so the effective default is None.
            if default is not inspect.Parameter.empty and not isinstance(default, _ParamMarker):
                required = False
                default_val = default
                has_default_val = True
            else:
                required = _marker_is_required(marker)
                default_val = None if required else marker.default
                has_default_val = not required
            alias = _compute_alias(name, marker)

            # Resolve the type hint from the (possibly unwrapped) annotation
            if _is_body_type(annotation):
                type_hint = "model"
                model_class = annotation
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
        elif _is_body_type(annotation):
            kind = "body"
            type_hint = "model"
            required = True
            default_val = None
            has_default_val = False
            model_class = annotation
        elif _is_upload_file_type(annotation):
            # Bare `f: UploadFile` or `f: list[UploadFile]` — infer file upload
            kind = "file"
            type_hint = "file"
            required = default is inspect.Parameter.empty
            default_val = None if required else default
            has_default_val = not required
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
        if _effective_marker is not None and kind in ("path", "query", "header", "cookie"):
            constraint_keys = (
                "gt", "ge", "lt", "le",
                "min_length", "max_length",
                "regex", "pattern",
                "multiple_of",
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
                    # If no direct attrs found, extract from FieldInfo.metadata
                    if not field_kwargs and hasattr(_effective_marker, "metadata"):
                        for meta in _effective_marker.metadata:
                            # annotated_types constraints: Ge(ge=0), Le(le=100), etc.
                            for k in constraint_keys:
                                v = getattr(meta, k, None)
                                if v is not None:
                                    field_kwargs["pattern" if k == "regex" else k] = v
                    # Use the UNWRAPPED type (int / str / etc.)
                    base_type = annotation if annotation is not inspect.Parameter.empty else str
                    field_obj = _PField(**field_kwargs)
                    scalar_validator = _PTypeAdapter(
                        _typing.Annotated[base_type, field_obj]
                    )
                except Exception:
                    scalar_validator = None

        # Track embed flag for body params
        embed = False
        media_type_override = None
        if marker is not None and isinstance(marker, Body):
            embed = getattr(marker, "embed", False)
            media_type_override = getattr(marker, "media_type", None)

        # Propagate example/examples/description from markers for OpenAPI
        example_val = None
        examples_val = None
        title_val = None
        description_val = None
        include_in_schema_val = True
        deprecated_val = None
        if marker is not None:
            example_val = getattr(marker, "example", None)
            examples_val = getattr(marker, "examples", None)
            title_val = getattr(marker, "title", None)
            description_val = getattr(marker, "description", None)
            include_in_schema_val = getattr(marker, "include_in_schema", True)
            deprecated_val = getattr(marker, "deprecated", None)

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
                "title": title_val,
                "description": description_val,
                "include_in_schema": include_in_schema_val,
                "deprecated": deprecated_val,
                "scalar_validator": scalar_validator,
                "enum_class": annotation if (
                    isinstance(annotation, type)
                    and issubclass(annotation, __import__("enum").Enum)
                ) else None,
            }
        )

    # Post-processing: handle multiple body params and Body(embed=True)
    params = _maybe_embed_body_params(params, endpoint)

    return params


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
            for meta in args[1:]:
                if isinstance(meta, _ParamMarker):
                    return meta, inner_type
            return None, inner_type
        return None, args[0] if args else annotation

    return None, annotation


def _compute_alias(name: str, marker: _ParamMarker) -> str | None:
    """Compute the lookup alias for a parameter.

    For Header params with convert_underscores=True (default), convert
    underscores in the Python name to hyphens.  If the user provides an
    explicit alias, use that instead.
    """
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
        query/form/header repetition collected into a Python list.
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
        # list[X] / List[X] / tuple[X, ...] / set[X]
        if origin in (list, tuple, set, frozenset):
            args = typing.get_args(annotation) or getattr(annotation, "__args__", ())
            inner = args[0] if args else str
            inner_name = _get_type_name(inner)
            return f"list_{inner_name}"

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


def _is_body_type(annotation) -> bool:
    """Return True if the annotation is a Pydantic BaseModel subclass."""
    if annotation is inspect.Parameter.empty or annotation is None:
        return False

    try:
        from pydantic import BaseModel

        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return True
    except ImportError:
        pass

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

    # Need to embed: create a wrapper model
    from pydantic import create_model

    field_definitions = {}
    for bp in body_params:
        model_cls = bp.get("model_class")
        if model_cls is not None:
            if bp["required"]:
                field_definitions[bp["name"]] = (model_cls, ...)
            else:
                field_definitions[bp["name"]] = (model_cls, bp["default_value"])
        else:
            # Non-model body params (plain types with Body marker)
            type_map = {"int": int, "float": float, "bool": bool, "str": str}
            py_type = type_map.get(bp["type_hint"], Any)
            if bp["required"]:
                field_definitions[bp["name"]] = (py_type, ...)
            else:
                field_definitions[bp["name"]] = (py_type, bp["default_value"])

    CombinedBody = create_model("_CombinedBody", **field_definitions)

    # Build the new params list: keep non-body params, replace body params
    # with a single combined body param named "_combined_body"
    new_params = [p for p in params if p["kind"] != "body"]
    new_params.append({
        "name": "_combined_body",
        "kind": "body",
        "type_hint": "model",
        "required": True,
        "default_value": None,
        "has_default": False,
        "model_class": CombinedBody,
        "alias": None,
        "_embed": False,
        "_body_param_names": [bp["name"] for bp in body_params],
        # The unwrap wrapper needs this to appear in handler_param_names so
        # the filtered kwargs include `_combined_body`, which the wrapper
        # then splits back into the individual body param names.
        "_is_handler_param": True,
    })

    return new_params
