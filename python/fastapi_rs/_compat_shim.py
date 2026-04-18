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
