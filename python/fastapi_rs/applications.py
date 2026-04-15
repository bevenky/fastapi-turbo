"""The main FastAPI-compatible application class."""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from typing import Any, Callable, Sequence

from fastapi_rs._introspect import introspect_endpoint
from fastapi_rs._openapi import generate_openapi_schema
from fastapi_rs._resolution import build_resolution_plan, _make_sync_wrapper
from fastapi_rs.routing import APIRouter


def _try_compile_handler(endpoint, params):
    """Compile deps + handler into a SINGLE Python function (1 PyO3 call instead of N+1).

    At startup, if ALL deps are trivially-sync (no generators, no real async I/O),
    we generate a function that resolves deps inline and calls the handler.
    Rust makes ONE call with extracted params → gets back the response.
    """
    import asyncio

    dep_steps = [p for p in params if p["kind"] == "dependency"]
    if not dep_steps:
        return None

    for dep in dep_steps:
        if dep.get("is_generator_dep"):
            return None  # Generator deps need cleanup — can't inline

    handler_param_names = {p["name"] for p in params if p.get("_is_handler_param")}

    # Prepare dep callables (wrap async → sync)
    dep_chain = []
    for dep in dep_steps:
        func = dep["dep_callable"]
        if asyncio.iscoroutinefunction(func):
            func = _make_sync_wrapper(func)
        dep_chain.append((
            dep["name"],
            func,
            dep.get("dep_input_map", []),
            dep.get("dep_callable_id"),
        ))

    handler_func = endpoint
    if asyncio.iscoroutinefunction(handler_func):
        handler_func = _make_sync_wrapper(handler_func)

    def _compiled(**kwargs):
        resolved = kwargs
        cache = {}
        for name, func, input_map, func_id in dep_chain:
            if func_id is not None and func_id in cache:
                resolved[name] = cache[func_id]
                continue
            dk = {pn: resolved[sk] for pn, sk in input_map if sk in resolved}
            result = func(**dk)
            resolved[name] = result
            if func_id is not None:
                cache[func_id] = result
        return handler_func(**{k: resolved[k] for k in handler_param_names if k in resolved})

    return _compiled


class FastAPI:
    """Drop-in replacement for ``fastapi.FastAPI``, backed by Rust Axum."""

    def __init__(
        self,
        *,
        title: str = "FastAPI",
        description: str = "",
        version: str = "0.1.0",
        docs_url: str | None = "/docs",
        redoc_url: str | None = "/redoc",
        openapi_url: str | None = "/openapi.json",
        lifespan=None,
        **kwargs: Any,
    ):
        self.title = title
        self.description = description
        self.version = version
        self.docs_url = docs_url
        self.redoc_url = redoc_url
        self.openapi_url = openapi_url
        self.lifespan = lifespan

        self.router = APIRouter()
        self.state = SimpleNamespace()
        self.dependency_overrides: dict[Callable, Callable] = {}

        self._middleware_stack: list[tuple[type, dict[str, Any]]] = []
        self._on_startup: list[Callable] = []
        self._on_shutdown: list[Callable] = []
        self._included_routers: list[tuple[APIRouter, str, list[str]]] = []

        self.extra = kwargs

    # ------------------------------------------------------------------
    # HTTP-method decorators — delegate straight to the root router
    # ------------------------------------------------------------------

    def get(self, path: str, **kwargs: Any):
        return self.router.get(path, **kwargs)

    def post(self, path: str, **kwargs: Any):
        return self.router.post(path, **kwargs)

    def put(self, path: str, **kwargs: Any):
        return self.router.put(path, **kwargs)

    def delete(self, path: str, **kwargs: Any):
        return self.router.delete(path, **kwargs)

    def patch(self, path: str, **kwargs: Any):
        return self.router.patch(path, **kwargs)

    def options(self, path: str, **kwargs: Any):
        return self.router.options(path, **kwargs)

    def head(self, path: str, **kwargs: Any):
        return self.router.head(path, **kwargs)

    # ------------------------------------------------------------------
    # WebSocket decorator
    # ------------------------------------------------------------------

    def websocket(self, path: str, **kwargs: Any):
        return self.router.websocket(path, **kwargs)

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
        """Register a child router for later flattening."""
        self._included_routers.append((router, prefix, tags or []))

    # ------------------------------------------------------------------
    # Middleware
    # ------------------------------------------------------------------

    def add_middleware(self, middleware_cls, **kwargs: Any) -> None:
        self._middleware_stack.append((middleware_cls, kwargs))

    def _build_middleware_config(self) -> list[dict[str, Any]]:
        """Convert the middleware stack into dicts the Rust core can consume."""
        config: list[dict[str, Any]] = []
        for cls, kwargs in self._middleware_stack:
            if isinstance(cls, str):
                # String shorthand: app.add_middleware("cors", allow_origins=["*"])
                config.append({"type": cls, **kwargs})
            elif hasattr(cls, "_fastapi_rs_middleware_type"):
                # Jamun middleware class with a known Tower mapping
                config.append({"type": cls._fastapi_rs_middleware_type, **kwargs})
            # else: unknown ASGI middleware — skip for now
        return config

    # ------------------------------------------------------------------
    # Lifecycle events
    # ------------------------------------------------------------------

    def on_event(self, event_type: str):
        """Decorator to register startup/shutdown handlers."""

        def decorator(func: Callable) -> Callable:
            if event_type == "startup":
                self._on_startup.append(func)
            elif event_type == "shutdown":
                self._on_shutdown.append(func)
            return func

        return decorator

    # ------------------------------------------------------------------
    # Route collection & introspection
    # ------------------------------------------------------------------

    def _collect_routes_from_router(
        self,
        router: APIRouter,
        prefix: str = "",
        extra_tags: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Recursively flatten a router tree into a list of route dicts."""
        extra_tags = extra_tags or []
        collected: list[dict[str, Any]] = []

        full_prefix = prefix + router.prefix

        for route in router.routes:
            full_path = full_prefix + route.path
            # Normalise double slashes but keep leading slash
            full_path = "/" + full_path.strip("/") if full_path != "/" else "/"

            is_websocket = getattr(route, "_is_websocket", False)

            if is_websocket:
                # WebSocket routes don't go through normal param introspection.
                # The handler receives a single WebSocket argument injected by Rust.
                collected.append(
                    {
                        "path": full_path,
                        "methods": ["GET"],
                        "endpoint": route.endpoint,
                        "is_async": inspect.iscoroutinefunction(route.endpoint),
                        "handler_name": route.name,
                        "tags": extra_tags + route.tags,
                        "params": [],
                        "is_websocket": True,
                    }
                )
                continue

            params = introspect_endpoint(route.endpoint, full_path)

            # Check if any params are dependencies
            has_deps = any(p["kind"] == "dependency" for p in params)

            if has_deps:
                params = build_resolution_plan(route.endpoint, full_path)
            else:
                for p in params:
                    p["_is_handler_param"] = True

            endpoint = route.endpoint
            is_async = inspect.iscoroutinefunction(endpoint)

            # KEY OPTIMIZATION: Compile deps + handler into a SINGLE Python function.
            # This reduces N+1 PyO3 boundary crossings to just 1.
            # Rust calls one function with extracted params → gets back the response.
            if has_deps:
                compiled = _try_compile_handler(endpoint, params)
                if compiled is not None:
                    # Success — all deps resolved inline in one function.
                    # Strip dep steps from params (Rust won't resolve them separately).
                    params = [p for p in params if p["kind"] != "dependency"]
                    for p in params:
                        p["_is_handler_param"] = True
                    endpoint = compiled
                    is_async = False  # Compiled handler is sync
                    has_deps = False  # No more dep steps for Rust
                else:
                    # Fallback: async wrapper for non-compilable deps
                    if is_async and not inspect.isasyncgenfunction(endpoint):
                        from fastapi_rs._resolution import _make_sync_wrapper
                        endpoint = _make_sync_wrapper(endpoint)
                        is_async = False

            collected.append(
                {
                    "path": full_path,
                    "methods": route.methods,
                    "endpoint": endpoint,
                    "is_async": is_async,
                    "handler_name": route.name,
                    "tags": extra_tags + route.tags,
                    "params": params,
                    "is_websocket": False,
                }
            )

        # Recurse into child routers
        for child_router, child_prefix, child_tags in router._included_routers:
            collected.extend(
                self._collect_routes_from_router(
                    child_router,
                    prefix=full_prefix + child_prefix,
                    extra_tags=extra_tags + child_tags,
                )
            )

        return collected

    def _collect_all_routes(self) -> list[dict[str, Any]]:
        """Walk the root router and all included routers, returning a flat list."""
        # Routes registered directly on self.router
        all_routes = self._collect_routes_from_router(self.router)

        # Routers added via app.include_router(...)
        for router, prefix, tags in self._included_routers:
            all_routes.extend(
                self._collect_routes_from_router(router, prefix=prefix, extra_tags=tags)
            )

        return all_routes

    # ------------------------------------------------------------------
    # OpenAPI schema
    # ------------------------------------------------------------------

    def openapi(self) -> dict[str, Any]:
        """Return the OpenAPI schema dict (cached after first call)."""
        if not hasattr(self, "_openapi_schema"):
            route_dicts = self._collect_all_routes()
            self._openapi_schema = generate_openapi_schema(
                title=self.title,
                version=self.version,
                description=self.description,
                routes=route_dicts,
            )
        return self._openapi_schema

    # ------------------------------------------------------------------
    # Server launch
    # ------------------------------------------------------------------

    def run(self, host: str = "127.0.0.1", port: int = 8000, **kwargs: Any) -> None:
        """Collect routes, hand them to the Rust core, and start serving."""
        from fastapi_rs._fastapi_rs_core import ParamInfo, RouteInfo, run_server

        route_dicts = self._collect_all_routes()
        route_infos: list[RouteInfo] = []

        for rd in route_dicts:
            param_infos = []
            for p in rd["params"]:
                pi = ParamInfo(
                    name=p["name"],
                    kind=p["kind"],
                    type_hint=p["type_hint"],
                    required=p["required"],
                    default_value=p["default_value"],
                    model_class=p.get("model_class"),
                    alias=p.get("alias"),
                    dep_callable=p.get("dep_callable"),
                    dep_callable_id=p.get("dep_callable_id"),
                    is_async_dep=p.get("is_async_dep", False),
                    is_generator_dep=p.get("is_generator_dep", False),
                    dep_input_names=p.get("dep_input_map", []),
                    is_handler_param=p.get("_is_handler_param", True),
                )
                param_infos.append(pi)

            route_infos.append(
                RouteInfo(
                    path=rd["path"],
                    methods=rd["methods"],
                    handler=rd["endpoint"],
                    is_async=rd["is_async"],
                    handler_name=rd["handler_name"],
                    params=param_infos,
                    is_websocket=rd.get("is_websocket", False),
                )
            )

        # Generate the OpenAPI schema JSON if docs are enabled
        openapi_json: str | None = None
        if self.openapi_url is not None:
            http_routes = [r for r in route_dicts if not r.get("is_websocket")]
            openapi_schema = generate_openapi_schema(
                title=self.title,
                version=self.version,
                description=self.description,
                routes=http_routes,
            )
            openapi_json = json.dumps(openapi_schema)

        middleware_config = self._build_middleware_config()
        run_server(
            route_infos,
            host,
            port,
            middleware_config,
            openapi_json,
            self.docs_url,
            self.redoc_url,
            self.openapi_url,
        )
