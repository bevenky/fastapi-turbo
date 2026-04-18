"""Routing primitives matching FastAPI's interface."""

from __future__ import annotations

import re
from typing import Any, Callable, Sequence
from urllib.parse import quote


def _default_generate_unique_id(route: "APIRoute", method: str) -> str:
    """Default function to generate a unique operation ID for OpenAPI."""
    return f"{route.name}_{method.lower()}"


class APIRoute:
    """Metadata for a single registered route."""

    def __init__(
        self,
        path: str,
        endpoint: Callable,
        *,
        methods: list[str] | None = None,
        response_model: Any = None,
        response_model_include: set | None = None,
        response_model_exclude: set | None = None,
        response_model_exclude_unset: bool = False,
        response_model_exclude_defaults: bool = False,
        response_model_exclude_none: bool = False,
        response_model_by_alias: bool = True,
        status_code: int | None = None,
        tags: list[str] | None = None,
        summary: str | None = None,
        description: str | None = None,
        response_description: str = "Successful Response",
        responses: dict | None = None,
        name: str | None = None,
        deprecated: bool | None = None,
        operation_id: str | None = None,
        generate_unique_id_function: Callable | None = None,
        dependencies: Sequence | None = None,
        response_class: Any = None,
        include_in_schema: bool = True,
        openapi_extra: dict | None = None,
        security: list | None = None,
        callbacks: list | None = None,
        servers: list[dict[str, Any]] | None = None,
        external_docs: dict[str, Any] | None = None,
        **kwargs: Any,
    ):
        self.path = path
        self.endpoint = endpoint
        self.methods = [m.upper() for m in (methods or ["GET"])]
        self.response_model = response_model
        self.response_model_include = response_model_include
        self.response_model_exclude = response_model_exclude
        self.response_model_exclude_unset = response_model_exclude_unset
        self.response_model_exclude_defaults = response_model_exclude_defaults
        self.response_model_exclude_none = response_model_exclude_none
        self.response_model_by_alias = response_model_by_alias
        self.status_code = status_code
        self.tags = tags or []
        self.summary = summary
        self.description = description
        self.response_description = response_description
        self.responses = responses or {}
        self.name = name or endpoint.__name__
        self.deprecated = bool(deprecated) if deprecated is not None else False
        self.dependencies = list(dependencies or [])
        self.response_class = response_class
        self.include_in_schema = include_in_schema
        self.openapi_extra = openapi_extra or {}
        self.security = security  # None = auto-derive; [] = disable; non-empty = override
        self.callbacks = callbacks or []
        # Per-operation servers / externalDocs (OpenAPI 3.1)
        self.servers = servers  # None = inherit from app
        self.external_docs = external_docs

        # Generate operation_id using the provided function or explicit value
        if operation_id is not None:
            self.operation_id = operation_id
        elif generate_unique_id_function is not None:
            self.operation_id = generate_unique_id_function(self, self.methods[0] if self.methods else "get")
        else:
            self.operation_id = None
        self.generate_unique_id_function = generate_unique_id_function


class APIRouter:
    """Route collection that mirrors FastAPI's APIRouter."""

    def __init__(
        self,
        *,
        prefix: str = "",
        tags: list[str] | None = None,
        dependencies: Sequence | None = None,
        default_response_class: Any = None,
        responses: dict | None = None,
        deprecated: bool | None = None,
        include_in_schema: bool = True,
        callbacks: list | None = None,
        generate_unique_id_function: Callable | None = None,
        route_class: type | None = None,
        redirect_slashes: bool = True,
        on_startup: Sequence[Callable] | None = None,
        on_shutdown: Sequence[Callable] | None = None,
        lifespan: Any = None,
        dependency_overrides_provider: Any = None,
        default: Any = None,
        **kwargs: Any,
    ):
        self.routes: list[APIRoute] = []
        self._included_routers: list[tuple[APIRouter, str, list[str], dict]] = []
        self.prefix = prefix
        self.tags = tags or []
        self.dependencies = list(dependencies or [])
        self.default_response_class = default_response_class
        self.responses = responses or {}
        self.deprecated = deprecated
        self.include_in_schema = include_in_schema
        self.callbacks = callbacks or []
        self.generate_unique_id_function = generate_unique_id_function
        self.route_class = route_class
        self.redirect_slashes = redirect_slashes
        self._on_startup: list[Callable] = list(on_startup or [])
        self._on_shutdown: list[Callable] = list(on_shutdown or [])
        self.lifespan = lifespan
        self.dependency_overrides_provider = dependency_overrides_provider
        self.default = default
        self._mounts: list[tuple[str, Any, str | None]] = []

    # ------------------------------------------------------------------
    # Core registration
    # ------------------------------------------------------------------

    def add_api_route(
        self,
        path: str,
        endpoint: Callable,
        *,
        methods: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Create an APIRoute and append it to this router."""
        route = APIRoute(path, endpoint, methods=methods, **kwargs)
        self.routes.append(route)

    # ------------------------------------------------------------------
    # Decorator helpers (one per HTTP verb)
    # ------------------------------------------------------------------

    def _method_decorator(self, method: str, path: str, **kwargs: Any):
        """Return a decorator that registers the endpoint for *method*."""

        def decorator(func: Callable) -> Callable:
            self.add_api_route(path, func, methods=[method], **kwargs)
            return func

        return decorator

    def get(self, path: str, **kwargs: Any):
        return self._method_decorator("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any):
        return self._method_decorator("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any):
        return self._method_decorator("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any):
        return self._method_decorator("DELETE", path, **kwargs)

    def patch(self, path: str, **kwargs: Any):
        return self._method_decorator("PATCH", path, **kwargs)

    def options(self, path: str, **kwargs: Any):
        return self._method_decorator("OPTIONS", path, **kwargs)

    def head(self, path: str, **kwargs: Any):
        return self._method_decorator("HEAD", path, **kwargs)

    def trace(self, path: str, **kwargs: Any):
        return self._method_decorator("TRACE", path, **kwargs)

    def api_route(
        self, path: str, *, methods: list[str] | None = None, **kwargs: Any
    ):
        """FastAPI multi-method route decorator.

        Used by SGLang::

            @app.api_route("/health", methods=["GET", "POST"])
            async def health(): ...
        """

        def decorator(func: Callable) -> Callable:
            self.add_api_route(path, func, methods=methods, **kwargs)
            return func

        return decorator

    # ------------------------------------------------------------------
    # Generic route decorator and imperative registration
    # ------------------------------------------------------------------

    def route(self, path: str, methods: list[str] | None = None, **kwargs: Any):
        """Generic route decorator (Starlette-compatible).

        Usage::

            @router.route("/health", methods=["GET", "POST"])
            async def health(request): ...
        """

        def decorator(func: Callable) -> Callable:
            self.add_api_route(path, func, methods=methods, **kwargs)
            return func

        return decorator

    def add_route(
        self,
        path: str,
        endpoint: Callable,
        methods: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Imperative generic route registration (Starlette-compatible)."""
        self.add_api_route(path, endpoint, methods=methods, **kwargs)

    # ------------------------------------------------------------------
    # WebSocket routes
    # ------------------------------------------------------------------

    def add_websocket_route(
        self,
        path: str,
        endpoint: Callable,
        *,
        name: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Register a WebSocket route."""
        route = APIRoute(
            path,
            endpoint,
            methods=["GET"],
            name=name,
            **kwargs,
        )
        # Tag it so the application layer can distinguish WS routes.
        route._is_websocket = True
        self.routes.append(route)

    def add_api_websocket_route(
        self,
        path: str,
        endpoint: Callable,
        *,
        name: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Imperative form of @router.websocket (alias for add_websocket_route)."""
        self.add_websocket_route(path, endpoint, name=name, **kwargs)

    def websocket(self, path: str, **kwargs: Any):
        """Decorator to register a WebSocket endpoint."""

        def decorator(func: Callable) -> Callable:
            self.add_websocket_route(path, func, **kwargs)
            return func

        return decorator

    def websocket_route(self, path: str, name: str | None = None, **kwargs: Any):
        """Decorator to register a WebSocket endpoint (returns the callable).

        Unlike ``websocket()``, this mirrors Starlette's ``websocket_route``
        which returns the original callable for further use.
        """

        def decorator(func: Callable) -> Callable:
            self.add_websocket_route(path, func, name=name, **kwargs)
            return func

        return decorator

    # ------------------------------------------------------------------
    # Lifecycle events
    # ------------------------------------------------------------------

    def on_event(self, event_type: str):
        """Decorator to register startup/shutdown handlers on this router."""

        def decorator(func: Callable) -> Callable:
            if event_type == "startup":
                self._on_startup.append(func)
            elif event_type == "shutdown":
                self._on_shutdown.append(func)
            return func

        return decorator

    def add_event_handler(self, event_type: str, func: Callable) -> None:
        """Imperative form of on_event — register a startup/shutdown handler."""
        if event_type == "startup":
            self._on_startup.append(func)
        elif event_type == "shutdown":
            self._on_shutdown.append(func)

    # ------------------------------------------------------------------
    # Mount sub-applications
    # ------------------------------------------------------------------

    def mount(self, path: str, app: Any = None, *, name: str | None = None) -> None:
        """Mount a sub-application or static files at the given path prefix."""
        self._mounts.append((path, app, name))

    # ------------------------------------------------------------------
    # URL building
    # ------------------------------------------------------------------

    def url_path_for(self, name: str, /, **path_params: Any) -> str:
        """Search routes by name and return the URL path with params filled in.

        Raises ``LookupError`` if no route with the given name is found.
        """
        for route in self.routes:
            if route.name == name:
                path = self.prefix + route.path

                def _sub(match: re.Match) -> str:
                    pname = match.group(1).split(":")[0]
                    if pname not in path_params:
                        raise KeyError(
                            f"Missing path param {pname!r} for route {name!r}"
                        )
                    val = path_params[pname]
                    if ":path" in match.group(0):
                        return str(val)
                    return quote(str(val), safe="")

                return re.sub(r"\{([^}]+)\}", _sub, path)

        # Search included routers recursively
        for child_router, child_prefix, _tags, _meta in self._included_routers:
            try:
                child_path = child_router.url_path_for(name, **path_params)
                return self.prefix + child_prefix + child_path
            except LookupError:
                continue

        raise LookupError(f"No route named {name!r}")

    # ------------------------------------------------------------------
    # Sub-router inclusion
    # ------------------------------------------------------------------

    def include_router(
        self,
        router: APIRouter,
        *,
        prefix: str = "",
        tags: list[str] | None = None,
        dependencies: Sequence | None = None,
        responses: dict | None = None,
        deprecated: bool | None = None,
        include_in_schema: bool = True,
        default_response_class: Any = None,
        callbacks: list | None = None,
        generate_unique_id_function: Callable | None = None,
    ) -> None:
        """Store a child router for later flattening."""
        include_meta = {
            "prefix": prefix,
            "tags": tags or [],
            "dependencies": list(dependencies or []),
            "responses": responses or {},
            "deprecated": deprecated,
            "include_in_schema": include_in_schema,
            "default_response_class": default_response_class,
        }
        self._included_routers.append((router, prefix, tags or [], include_meta))
