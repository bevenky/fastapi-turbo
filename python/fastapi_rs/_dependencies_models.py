"""Dependant model matching ``fastapi.dependencies.models.Dependant``.

This provides the full dataclass that third-party libraries (e.g. FastAPI
dependency introspection tools) expect when importing from
``fastapi.dependencies.models`` or ``fastapi.dependencies.utils``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Dependant:
    """Dependency resolution model matching FastAPI's internal Dependant class.

    Fields mirror FastAPI's ``fastapi.dependencies.models.Dependant`` so that
    third-party plugins that inspect dependency graphs continue to work.
    """

    path_params: list[Any] = field(default_factory=list)
    query_params: list[Any] = field(default_factory=list)
    header_params: list[Any] = field(default_factory=list)
    cookie_params: list[Any] = field(default_factory=list)
    body_params: list[Any] = field(default_factory=list)
    dependencies: list[Any] = field(default_factory=list)
    security_requirements: list[Any] = field(default_factory=list)
    name: str | None = None
    call: Callable[..., Any] | None = None
    request_param_name: str | None = None
    websocket_param_name: str | None = None
    response_param_name: str | None = None
    background_tasks_param_name: str | None = None
    security_scopes_param_name: str | None = None
    security_scopes: list[str] | None = None
    use_cache: bool = True
    path: str | None = None
