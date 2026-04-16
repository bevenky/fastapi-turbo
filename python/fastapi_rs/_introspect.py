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

# Pattern to extract {param_name} from path strings like "/users/{user_id}/posts/{post_id}"
_PATH_PARAM_RE = re.compile(r"\{(\w+)\}")


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
        annotation = hints.get(name, param.annotation)
        default = param.default
        model_class = None
        alias = None

        # Check for Depends marker BEFORE other markers
        dep_marker = _extract_depends_marker(annotation, default)
        if dep_marker is not None:
            params.append(
                {
                    "name": name,
                    "kind": "dependency",
                    "type_hint": "any",
                    "required": False,
                    "default_value": None,
                    "model_class": None,
                    "alias": None,
                    "dep_callable": dep_marker.dependency,
                    "use_cache": dep_marker.use_cache,
                }
            )
            continue

        # Check for Annotated[T, marker] pattern
        marker, inner_type = _extract_annotated_marker(annotation)

        if marker is not None:
            # Use the inner type from Annotated for type resolution
            annotation = inner_type

        # If no marker from Annotated, check if default is a marker
        if marker is None and isinstance(default, _ParamMarker):
            marker = default

        if marker is not None:
            kind = marker._kind
            # required if marker.default is ... (Ellipsis)
            required = marker.default is ...
            default_val = None if required else marker.default
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
        elif _is_body_type(annotation):
            kind = "body"
            type_hint = "model"
            required = True
            default_val = None
            model_class = annotation
        else:
            kind = "query"
            type_hint = _get_type_name(annotation)
            required = default is inspect.Parameter.empty
            default_val = None if required else default

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
    # Check default value first
    if isinstance(default, Depends):
        return default

    # Check Annotated[T, Depends(...)]
    origin = getattr(annotation, "__origin__", None)
    if origin is None:
        return None

    annotated_type = getattr(typing, "Annotated", None)
    if annotated_type is None:
        try:
            from typing_extensions import Annotated as annotated_type
        except ImportError:
            return None

    if origin is annotated_type:
        args = getattr(annotation, "__args__", ())
        for meta in args[1:]:
            if isinstance(meta, Depends):
                return meta

    return None


def _extract_annotated_marker(annotation) -> tuple[_ParamMarker | None, Any]:
    """If annotation is Annotated[T, marker], return (marker, T). Else (None, annotation)."""
    origin = getattr(annotation, "__origin__", None)
    if origin is None:
        return None, annotation

    # Check for typing.Annotated (Python 3.9+: typing.Annotated, 3.8: typing_extensions)
    annotated_type = getattr(typing, "Annotated", None)
    if annotated_type is None:
        try:
            from typing_extensions import Annotated as annotated_type  # type: ignore[assignment]
        except ImportError:
            return None, annotation

    # typing.get_origin(Annotated[X, ...]) returns Annotated in 3.11+
    # For earlier versions, __origin__ may differ; check __class_getitem__ origin
    if origin is annotated_type:
        args = annotation.__args__
        if len(args) >= 2:
            inner_type = args[0]
            for meta in args[1:]:
                if isinstance(meta, _ParamMarker):
                    return meta, inner_type
        return None, args[0] if args else annotation

    # For Python 3.8-3.10, typing.Annotated may have a different origin
    # Also handle typing_extensions.Annotated
    try:
        import typing_extensions

        te_annotated = getattr(typing_extensions, "Annotated", None)
        if te_annotated is not None and origin is te_annotated:
            args = annotation.__args__
            if len(args) >= 2:
                inner_type = args[0]
                for meta in args[1:]:
                    if isinstance(meta, _ParamMarker):
                        return meta, inner_type
            return None, args[0] if args else annotation
    except ImportError:
        pass

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
    """Map a type annotation to a simple string the Rust side understands."""
    if annotation is inspect.Parameter.empty or annotation is None:
        return "str"

    # Handle Optional[X], Union[X, None], etc. by unwrapping
    origin = getattr(annotation, "__origin__", None)
    if origin is not None:
        if origin is typing.Union:
            args = [a for a in annotation.__args__ if a is not type(None)]
            if args:
                return _get_type_name(args[0])
            return "str"

    type_map: dict[type, str] = {
        int: "int",
        float: "float",
        bool: "bool",
        str: "str",
        bytes: "bytes",
    }

    if isinstance(annotation, type):
        return type_map.get(annotation, "str")

    return "str"


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
        "model_class": CombinedBody,
        "alias": None,
        "_embed": False,
        "_body_param_names": [bp["name"] for bp in body_params],
    })

    return new_params
