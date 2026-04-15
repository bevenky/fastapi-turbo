"""Routing primitives matching FastAPI's interface."""

from __future__ import annotations

from typing import Any, Callable, Sequence


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
        status_code: int | None = None,
        tags: list[str] | None = None,
        summary: str | None = None,
        description: str | None = None,
        name: str | None = None,
        deprecated: bool = False,
        operation_id: str | None = None,
        generate_unique_id_function: Callable | None = None,
        dependencies: Sequence | None = None,
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
        self.status_code = status_code
        self.tags = tags or []
        self.summary = summary
        self.description = description
        self.name = name or endpoint.__name__
        self.deprecated = deprecated
        self.dependencies = list(dependencies or [])

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
        **kwargs: Any,
    ):
        self.routes: list[APIRoute] = []
        self._included_routers: list[tuple[APIRouter, str, list[str]]] = []
        self.prefix = prefix
        self.tags = tags or []
        self.dependencies = list(dependencies or [])

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

    def websocket(self, path: str, **kwargs: Any):
        """Decorator to register a WebSocket endpoint."""

        def decorator(func: Callable) -> Callable:
            self.add_websocket_route(path, func, **kwargs)
            return func

        return decorator

    # ------------------------------------------------------------------
    # Sub-router inclusion
    # ------------------------------------------------------------------

    def include_router(
        self,
        router: APIRouter,
        *,
        prefix: str = "",
        tags: list[str] | None = None,
    ) -> None:
        """Store a child router for later flattening."""
        self._included_routers.append((router, prefix, tags or []))
