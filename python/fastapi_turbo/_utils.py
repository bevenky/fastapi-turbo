"""Utility functions matching ``fastapi.utils``.

These are used internally and by third-party plugins that import from
``fastapi.utils``.
"""

from __future__ import annotations

import re
from typing import Any


def deep_dict_update(main_dict: dict, update_dict: dict) -> dict:
    """Recursively merge *update_dict* into *main_dict*.

    Nested dicts are merged rather than replaced.  Returns *main_dict*
    (mutated in-place) for convenience.
    """
    for key, value in update_dict.items():
        if (
            isinstance(value, dict)
            and key in main_dict
            and isinstance(main_dict[key], dict)
        ):
            deep_dict_update(main_dict[key], value)
        else:
            main_dict[key] = value
    return main_dict


def is_body_allowed_for_status_code(status_code: int) -> bool:
    """Return True if the HTTP status code permits a response body."""
    if status_code < 200:
        return False
    if status_code in (204, 304):
        return False
    return True


def generate_operation_id_for_path(*, name: str, path: str, method: str) -> str:
    """Generate a deterministic operationId from route name, path, and method."""
    operation_id = f"{name}{path}"
    operation_id = operation_id.replace("/", "_").replace("{", "").replace("}", "")
    operation_id = f"{operation_id}_{method.lower()}"
    return operation_id


def get_path_param_names(path: str) -> set[str]:
    """Extract ``{param}`` names from a URL path template."""
    return set(re.findall(r"\{(\w+)(?::\w+)?\}", path))


def get_value_or_default(first_item: Any, *extra_items: Any) -> Any:
    """Resolve a DefaultPlaceholder chain, returning the first non-placeholder value.

    Used by FastAPI internals to distinguish "not explicitly set" from an
    actual value when merging router-level and route-level settings.
    """
    from fastapi_turbo.datastructures import DefaultPlaceholder

    items = (first_item,) + extra_items
    for item in items:
        if not isinstance(item, DefaultPlaceholder):
            return item
    return first_item


def generate_unique_id(route: Any) -> str:
    """Default operation ID generator matching FastAPI's default.

    Produces a string like ``read_items_items__get`` from the route's
    *name* and *path* attributes.
    """
    name = getattr(route, "name", None) or getattr(
        getattr(route, "endpoint", None), "__name__", "unknown"
    )
    path = getattr(route, "path", "/")
    operation_id = name + path
    operation_id = operation_id.replace("/", "_").strip("_")
    return operation_id
