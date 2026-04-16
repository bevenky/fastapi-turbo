"""The main FastAPI-compatible application class."""

from __future__ import annotations

import asyncio
import atexit
import inspect
import json
from types import SimpleNamespace
from typing import Any, Callable, Sequence

from fastapi_rs._introspect import introspect_endpoint
from fastapi_rs._openapi import generate_openapi_schema
from fastapi_rs._resolution import build_resolution_plan, _make_sync_wrapper
from fastapi_rs.routing import APIRouter


class URLPath(str):
    """Starlette-compatible URLPath — a str subclass with make_absolute_url()."""

    def __new__(cls, path: str, protocol: str = "", host: str = ""):
        instance = super().__new__(cls, path)
        instance.protocol = protocol
        instance.host = host
        return instance

    def make_absolute_url(self, base_url) -> str:
        base = str(base_url).rstrip("/")
        return base + str(self)


def _apply_response_model(
    result,
    response_model,
    include=None,
    exclude=None,
    exclude_unset=False,
    exclude_defaults=False,
    exclude_none=False,
):
    """Filter a handler result through a response_model Pydantic class."""
    if response_model is None or result is None:
        return result

    has_filters = include is not None or exclude is not None or exclude_unset or exclude_defaults or exclude_none

    try:
        if isinstance(result, dict):
            if not has_filters:
                # Fast path: validate only (no dump round-trip needed)
                # Just strip extra fields by keeping only model fields
                model_fields = response_model.model_fields
                return {k: v for k, v in result.items() if k in model_fields}
            validated = response_model.model_validate(result)
        elif hasattr(result, "model_dump"):
            if not has_filters and type(result) is response_model:
                # Already the right type — just dump
                return result.model_dump()
            validated = response_model.model_validate(
                result.model_dump() if hasattr(result, "model_dump") else result
            )
        else:
            return result

        dump_kwargs = {}
        if include is not None:
            dump_kwargs["include"] = include
        if exclude is not None:
            dump_kwargs["exclude"] = exclude
        if exclude_unset:
            dump_kwargs["exclude_unset"] = True
        if exclude_defaults:
            dump_kwargs["exclude_defaults"] = True
        if exclude_none:
            dump_kwargs["exclude_none"] = True
        return validated.model_dump(**dump_kwargs)
    except Exception:
        pass
    return result


def _wrap_response_class(result, response_class):
    """Wrap a bare handler result (dict/list/str/etc.) in a response_class.

    If the handler already returned a Response instance, leave it alone
    (Starlette semantics: user-returned Response always wins).
    """
    if response_class is None or result is None:
        return result
    # If result is already a Response-like object, don't double-wrap
    if hasattr(result, "status_code") and hasattr(result, "body"):
        return result
    return response_class(content=result)


def _try_compile_handler(
    endpoint,
    params,
    app=None,
    response_model=None,
    response_model_include=None,
    response_model_exclude=None,
    response_model_exclude_unset=False,
    response_model_exclude_defaults=False,
    response_model_exclude_none=False,
    response_class=None,
):
    """Compile deps + handler into a SINGLE Python function (1 PyO3 call instead of N+1).

    At startup, if ALL deps are trivially-sync (no generators, no real async I/O),
    we generate a function that resolves deps inline and calls the handler.
    Rust makes ONE call with extracted params -> gets back the response.

    Now also supports:
    - dependency_overrides: checks app.dependency_overrides at call time
    - generator deps (yield): runs generator, captures value, cleans up after handler
    - response_model: filters the response through the model if set
    """
    _rm_include = response_model_include
    _rm_exclude = response_model_exclude
    _rm_exclude_unset = response_model_exclude_unset
    _rm_exclude_defaults = response_model_exclude_defaults
    _rm_exclude_none = response_model_exclude_none
    _response_class = response_class

    dep_steps = [p for p in params if p["kind"] == "dependency"]
    _has_exc_handlers = app is not None and bool(getattr(app, "exception_handlers", None))
    if not dep_steps:
        if response_model is not None or _response_class is not None or _has_exc_handlers:
            # Even without deps, we may need response_model filtering or response_class wrapping
            handler_param_names = {p["name"] for p in params if p.get("_is_handler_param")}
            handler_func = endpoint
            if asyncio.iscoroutinefunction(handler_func):
                handler_func = _make_sync_wrapper(handler_func)

            _app_ref = app

            def _compiled_no_deps(**kwargs):
                try:
                    result = handler_func(**{k: kwargs[k] for k in handler_param_names if k in kwargs})
                except Exception as exc:
                    if _app_ref is not None and _app_ref.exception_handlers:
                        handler_result = _app_ref._invoke_exception_handler(exc)
                        if handler_result is not None:
                            return handler_result
                    raise
                if response_model is not None:
                    result = _apply_response_model(
                        result, response_model,
                        include=_rm_include, exclude=_rm_exclude,
                        exclude_unset=_rm_exclude_unset,
                        exclude_defaults=_rm_exclude_defaults,
                        exclude_none=_rm_exclude_none,
                    )
                if _response_class is not None:
                    result = _wrap_response_class(result, _response_class)
                return result

            return _compiled_no_deps
        return None

    handler_param_names = {p["name"] for p in params if p.get("_is_handler_param")}

    # Prepare dep callables (wrap async -> sync) and store originals for override lookup
    dep_chain = []
    for dep in dep_steps:
        original_func = dep.get("_original_dep_callable", dep["dep_callable"])
        func = dep["dep_callable"]
        is_generator = dep.get("is_generator_dep", False)
        if asyncio.iscoroutinefunction(func) and not is_generator:
            func = _make_sync_wrapper(func)
        dep_chain.append((
            dep["name"],
            func,
            original_func,
            dep.get("dep_input_map", []),
            dep.get("dep_callable_id"),
            is_generator,
        ))

    handler_func = endpoint
    if asyncio.iscoroutinefunction(handler_func):
        handler_func = _make_sync_wrapper(handler_func)

    # Capture app reference for override lookup at call time
    _app = app

    def _compiled(**kwargs):
        resolved = kwargs
        cache = {}
        generators_to_cleanup = []

        for name, func, original_func, input_map, func_id, is_generator in dep_chain:
            # Check dependency_overrides at call time (P0 fix #1)
            actual_func = func
            if _app is not None and _app.dependency_overrides:
                override = _app.dependency_overrides.get(original_func)
                if override is not None:
                    actual_func = override
                    if asyncio.iscoroutinefunction(actual_func):
                        actual_func = _make_sync_wrapper(actual_func)

            if func_id is not None and func_id in cache:
                resolved[name] = cache[func_id]
                continue
            dk = {pn: resolved[sk] for pn, sk in input_map if sk in resolved}

            if is_generator:
                # Generator dep (yield) support (P0 fix #4)
                gen = actual_func(**dk)
                result = next(gen)
                generators_to_cleanup.append(gen)
            else:
                result = actual_func(**dk)

            resolved[name] = result
            if func_id is not None:
                cache[func_id] = result

        try:
            try:
                result = handler_func(**{k: resolved[k] for k in handler_param_names if k in resolved})
            except Exception as exc:
                # Route through app's exception_handlers if one is registered
                if _app is not None and _app.exception_handlers:
                    handler_result = _app._invoke_exception_handler(exc)
                    if handler_result is not None:
                        return handler_result
                raise
            # Apply response_model filtering (P0 fix #5)
            if response_model is not None:
                result = _apply_response_model(
                    result, response_model,
                    include=_rm_include, exclude=_rm_exclude,
                    exclude_unset=_rm_exclude_unset,
                    exclude_defaults=_rm_exclude_defaults,
                    exclude_none=_rm_exclude_none,
                )
            # Wrap in response_class if set
            if _response_class is not None:
                result = _wrap_response_class(result, _response_class)
            return result
        finally:
            # Clean up generator deps in reverse order (P0 fix #4)
            for gen in reversed(generators_to_cleanup):
                try:
                    next(gen)
                except StopIteration:
                    pass

    return _compiled


# Imports hoisted to module-level for the hot path (used by wrapped endpoints)
from fastapi_rs.requests import Request as _Request
from fastapi_rs.responses import JSONResponse as _JSONResponse


def _wrap_with_http_middlewares(endpoint, middlewares, app):
    """Wrap a route endpoint with a chain of @app.middleware("http") functions.

    FAST PATH: Drive the async middleware chain SYNCHRONOUSLY via coro.send(None).
    Most HTTP middlewares only `await call_next(request)` — they don't do real I/O.
    By making call_next an `async def` that returns immediately, the middleware's
    coroutine completes in one send() call, avoiding the expensive Rust async path
    (saves ~50μs per request).

    Falls back to the normal async path only if a middleware actually suspends
    on real I/O (rare in HTTP middleware — logging, header mangling, etc.).
    """
    if not middlewares:
        return endpoint

    is_async_endpoint = asyncio.iscoroutinefunction(endpoint)

    # Shared scope — recycled per request (shallow copy cheap)
    def _make_scope(kwargs):
        return {"type": "http", "app": app, "_handler_kwargs": kwargs}

    def _call_handler_sync(kwargs):
        """Run the underlying handler, returning a Response-normalized value."""
        if is_async_endpoint:
            coro = endpoint(**kwargs)
            try:
                coro.send(None)
                # Suspended — fall back
                coro.close()
                raise _MiddlewareSuspendedError()
            except StopIteration as e:
                result = e.value
        else:
            result = endpoint(**kwargs)
        # Normalize bare dict/list to a Response so middleware can mutate headers
        if result is None or hasattr(result, "status_code"):
            return result
        if isinstance(result, (dict, list)):
            return _JSONResponse(content=result)
        return result

    # Build a chain of sync callables. Each one drives its middleware via
    # coro.send(None) and returns the result. The innermost one calls the handler.
    def _make_runner(idx: int):
        """Return a function that runs middleware[idx] around the inner chain."""
        if idx >= len(middlewares):
            return None
        mw = middlewares[idx]
        inner = _make_runner(idx + 1)

        def _run_chain(request, kwargs):
            # Build a call_next that resolves synchronously via the next runner
            # (or the handler if we're at the end of the chain).
            async def call_next(_req=None):
                if inner is None:
                    return _call_handler_sync(kwargs)
                return inner(request, kwargs)

            if asyncio.iscoroutinefunction(mw):
                coro = mw(request, call_next)
                try:
                    coro.send(None)
                    # Middleware actually suspended on something other than our call_next.
                    # That shouldn't happen for typical HTTP middleware, but handle it.
                    coro.close()
                    raise _MiddlewareSuspendedError()
                except StopIteration as e:
                    return e.value
            else:
                # Sync middleware (rare)
                return mw(request, call_next)

        return _run_chain

    runner = _make_runner(0)

    def wrapped_sync(**kwargs):
        request = _Request(_make_scope(kwargs))
        try:
            return runner(request, kwargs)
        except _MiddlewareSuspendedError:
            # Fallback: drive everything through a fresh event loop
            return _drive_async_fallback(endpoint, middlewares, app, kwargs, is_async_endpoint)

    return wrapped_sync


class _MiddlewareSuspendedError(Exception):
    """Internal: raised when sync-driving fails because a middleware suspends."""
    pass


def _drive_async_fallback(endpoint, middlewares, app, kwargs, is_async_endpoint):
    """Fallback: run the whole middleware chain on a real asyncio event loop.

    Used when a middleware suspends on real I/O (e.g., httpx call inside).
    """
    async def _chain():
        request = _Request({"type": "http", "app": app, "_handler_kwargs": kwargs})

        async def call_handler():
            if is_async_endpoint:
                result = await endpoint(**kwargs)
            else:
                result = endpoint(**kwargs)
            if result is None or hasattr(result, "status_code"):
                return result
            if isinstance(result, (dict, list)):
                return _JSONResponse(content=result)
            return result

        async def build(idx):
            if idx >= len(middlewares):
                return await call_handler()
            mw = middlewares[idx]

            async def call_next(_req=None):
                return await build(idx + 1)

            if asyncio.iscoroutinefunction(mw):
                return await mw(request, call_next)
            return mw(request, call_next)

        return await build(0)

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_chain())
    finally:
        loop.close()


def _collect_dependencies_from_markers(dependencies):
    """Convert a list of Depends markers into introspection-ready param dicts."""
    from fastapi_rs.dependencies import Depends as DependsClass

    result = []
    for i, dep in enumerate(dependencies):
        if isinstance(dep, DependsClass):
            dep_func = dep.dependency
            result.append({
                "name": f"_global_dep_{i}_{id(dep_func)}",
                "kind": "dependency",
                "type_hint": "any",
                "required": False,
                "default_value": None,
                "model_class": None,
                "alias": None,
                "dep_callable": dep_func,
                "use_cache": dep.use_cache,
            })
    return result


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
        servers: list[dict[str, Any]] | None = None,
        terms_of_service: str | None = None,
        contact: dict[str, Any] | None = None,
        license_info: dict[str, Any] | None = None,
        openapi_tags: list[dict[str, Any]] | None = None,
        lifespan=None,
        dependencies: Sequence | None = None,
        root_path: str = "",
        root_path_in_servers: bool = True,
        exception_handlers: dict | None = None,
        **kwargs: Any,
    ):
        self.title = title
        self.description = description
        self.version = version
        self.docs_url = docs_url
        self.redoc_url = redoc_url
        self.openapi_url = openapi_url
        self.servers = servers
        self.terms_of_service = terms_of_service
        self.contact = contact
        self.license_info = license_info
        self.openapi_tags = openapi_tags
        self.lifespan = lifespan
        self.root_path = root_path
        self.root_path_in_servers = root_path_in_servers
        # Map of exception class (or int status code) -> handler callable
        self.exception_handlers: dict = dict(exception_handlers or {})

        self.router = APIRouter()
        self.state = SimpleNamespace()
        self.dependency_overrides: dict[Callable, Callable] = {}
        self.dependencies: list = list(dependencies or [])

        self._middleware_stack: list[tuple[type, dict[str, Any]]] = []
        # @app.middleware("http") registered middlewares — Python-side HTTP middlewares
        # that wrap each user route handler.
        self._http_middlewares: list[Callable] = []
        self._on_startup: list[Callable] = []
        self._on_shutdown: list[Callable] = []
        self._included_routers: list[tuple[APIRouter, str, list[str]]] = []
        self._mounts: list[tuple[str, Any, str | None]] = []

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

    def trace(self, path: str, **kwargs: Any):
        return self.router.trace(path, **kwargs)

    # ------------------------------------------------------------------
    # WebSocket decorator
    # ------------------------------------------------------------------

    def websocket(self, path: str, **kwargs: Any):
        return self.router.websocket(path, **kwargs)

    # ------------------------------------------------------------------
    # Mount sub-applications
    # ------------------------------------------------------------------

    def mount(self, path: str, app: Any = None, *, name: str | None = None) -> None:
        """Mount a sub-application or router at the given path prefix.

        Supports mounting FastAPI or APIRouter instances. Their routes
        are collected with *path* as a prefix during route collection.
        """
        self._mounts.append((path, app, name))

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

    def middleware(self, middleware_type: str):
        """Decorator to register a Python HTTP middleware (Starlette-compatible).

        Usage:
            @app.middleware("http")
            async def add_custom_header(request, call_next):
                response = await call_next(request)
                response.headers["x-custom"] = "value"
                return response

        Only middleware_type="http" is supported. The middleware wraps each
        user route handler (doesn't intercept Rust-native endpoints like /_ping).
        """
        if middleware_type != "http":
            raise ValueError(f"Unsupported middleware type: {middleware_type!r}; only 'http' is supported")

        def decorator(func: Callable) -> Callable:
            self._http_middlewares.append(func)
            return func

        return decorator

    def _build_middleware_config(self) -> list[dict[str, Any]]:
        """Convert the middleware stack into dicts the Rust core can consume."""
        from fastapi_rs.middleware.trustedhost import TrustedHostMiddleware
        from fastapi_rs.middleware.httpsredirect import HTTPSRedirectMiddleware

        config: list[dict[str, Any]] = []
        for cls, kwargs in self._middleware_stack:
            if isinstance(cls, str):
                # String shorthand: app.add_middleware("cors", allow_origins=["*"])
                config.append({"type": cls, **kwargs})
            elif isinstance(cls, type) and issubclass(cls, TrustedHostMiddleware):
                config.append({
                    "type": "trustedhost",
                    "allowed_hosts": kwargs.get("allowed_hosts", ["*"]),
                })
            elif isinstance(cls, type) and issubclass(cls, HTTPSRedirectMiddleware):
                config.append({"type": "httpsredirect"})
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
    # Exception handlers
    # ------------------------------------------------------------------

    def exception_handler(self, exc_class_or_status_code):
        """Register a handler for an exception class or HTTP status code.

        Usage:
            @app.exception_handler(HTTPException)
            async def handle(request, exc):
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

            @app.exception_handler(404)
            async def handle_404(request, exc):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
        """

        def decorator(func: Callable) -> Callable:
            self.exception_handlers[exc_class_or_status_code] = func
            return func

        return decorator

    def add_exception_handler(self, exc_class_or_status_code, handler: Callable) -> None:
        """Imperative form of @app.exception_handler()."""
        self.exception_handlers[exc_class_or_status_code] = handler

    def _lookup_exception_handler(self, exc: BaseException) -> Callable | None:
        """Look up a handler by exact class, then by MRO, then by status code.

        Matches Starlette's resolution order.
        """
        # Exact class first
        cls = type(exc)
        if cls in self.exception_handlers:
            return self.exception_handlers[cls]
        # Walk MRO (parent classes)
        for parent in cls.__mro__[1:]:
            if parent in self.exception_handlers:
                return self.exception_handlers[parent]
        # Status code match (for HTTPException subclasses)
        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, int) and status_code in self.exception_handlers:
            return self.exception_handlers[status_code]
        return None

    def _invoke_exception_handler(self, exc: BaseException):
        """Run a registered exception handler and return its Response-like result.

        Returns None if no handler is found. The caller is responsible for
        falling back to the default FastAPI error response.
        """
        handler = self._lookup_exception_handler(exc)
        if handler is None:
            return None
        # Build a minimal Request stub — handlers typically only use it for introspection
        from fastapi_rs.requests import Request
        request = Request({"type": "http", "app": self})
        try:
            if asyncio.iscoroutinefunction(handler):
                # Drive the coroutine via the send(None) trick (works for handlers
                # that don't actually suspend). Fall back to a new event loop otherwise.
                coro = handler(request, exc)
                try:
                    coro.send(None)
                except StopIteration as e:
                    return e.value
                # Coroutine suspended — need a real event loop
                coro.close()
                coro = handler(request, exc)
                try:
                    loop = asyncio.new_event_loop()
                    try:
                        return loop.run_until_complete(coro)
                    finally:
                        loop.close()
                except Exception:
                    return None
            return handler(request, exc)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Route collection & introspection
    # ------------------------------------------------------------------

    def _get_all_dependencies_for_route(self, router: APIRouter, route) -> list:
        """Merge app-level, router-level, and route-level dependencies (P0 fix #6)."""
        merged = []
        # App-level dependencies first
        merged.extend(self.dependencies)
        # Router-level dependencies
        merged.extend(router.dependencies)
        # Route-level dependencies
        merged.extend(route.dependencies)
        return merged

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

            # Merge global/router-level/route-level dependencies (P0 fix #6)
            merged_deps = self._get_all_dependencies_for_route(router, route)

            # Check if any params are dependencies (including merged ones)
            has_deps = any(p["kind"] == "dependency" for p in params) or bool(merged_deps)

            if has_deps:
                params = build_resolution_plan(route.endpoint, full_path, extra_deps=merged_deps)
            else:
                for p in params:
                    p["_is_handler_param"] = True

            # Store original dep callable references for override lookup
            for p in params:
                if p["kind"] == "dependency" and "_original_dep_callable" not in p:
                    p["_original_dep_callable"] = p.get("dep_callable")

            # Save all params (including deps) for OpenAPI security scheme detection
            all_params_for_openapi = list(params)

            endpoint = route.endpoint
            is_async = inspect.iscoroutinefunction(endpoint)
            response_model = getattr(route, "response_model", None)
            rm_include = getattr(route, "response_model_include", None)
            rm_exclude = getattr(route, "response_model_exclude", None)
            rm_exclude_unset = getattr(route, "response_model_exclude_unset", False)
            rm_exclude_defaults = getattr(route, "response_model_exclude_defaults", False)
            rm_exclude_none = getattr(route, "response_model_exclude_none", False)
            response_class = getattr(route, "response_class", None)

            rm_kwargs = dict(
                response_model_include=rm_include,
                response_model_exclude=rm_exclude,
                response_model_exclude_unset=rm_exclude_unset,
                response_model_exclude_defaults=rm_exclude_defaults,
                response_model_exclude_none=rm_exclude_none,
                response_class=response_class,
            )

            # KEY OPTIMIZATION: Compile deps + handler into a SINGLE Python function.
            # This reduces N+1 PyO3 boundary crossings to just 1.
            # Rust calls one function with extracted params -> gets back the response.
            if has_deps:
                compiled = _try_compile_handler(
                    endpoint, params, app=self, response_model=response_model,
                    **rm_kwargs,
                )
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
            elif response_model is not None or response_class is not None or self.exception_handlers:
                # No deps but has response_model/response_class/exception_handlers -- wrap handler
                compiled = _try_compile_handler(
                    endpoint, params, app=self, response_model=response_model,
                    **rm_kwargs,
                )
                if compiled is not None:
                    endpoint = compiled
                    is_async = False

            # Wrap handler when multiple body params are combined
            combined = [p for p in params if p.get("name") == "_combined_body" and p.get("_body_param_names")]
            if combined:
                body_param_names = combined[0]["_body_param_names"]
                original_endpoint = endpoint
                original_is_async = is_async

                if inspect.iscoroutinefunction(original_endpoint):
                    async def _unwrap_combined_async(
                        _body_names=body_param_names,
                        _orig=original_endpoint,
                        **kwargs,
                    ):
                        combined_body = kwargs.pop("_combined_body", None)
                        if combined_body is not None:
                            for bname in _body_names:
                                kwargs[bname] = getattr(combined_body, bname)
                        return await _orig(**kwargs)

                    endpoint = _unwrap_combined_async
                    is_async = True
                else:
                    def _unwrap_combined_sync(
                        _body_names=body_param_names,
                        _orig=original_endpoint,
                        **kwargs,
                    ):
                        combined_body = kwargs.pop("_combined_body", None)
                        if combined_body is not None:
                            for bname in _body_names:
                                kwargs[bname] = getattr(combined_body, bname)
                        return _orig(**kwargs)

                    endpoint = _unwrap_combined_sync
                    is_async = False

            # Apply @app.middleware("http") chain around the endpoint.
            # The wrapper drives the chain SYNCHRONOUSLY (via coro.send) so we
            # stay on the fast Rust sync path — same perf as an unwrapped route.
            if self._http_middlewares:
                endpoint = _wrap_with_http_middlewares(endpoint, self._http_middlewares, self)
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
                    "_all_params": all_params_for_openapi,
                    "is_websocket": False,
                    # OpenAPI metadata
                    "status_code": route.status_code or 200,
                    "summary": route.summary,
                    "description": route.description,
                    "response_description": getattr(route, "response_description", "Successful Response"),
                    "responses": getattr(route, "responses", {}),
                    "response_model": response_model,
                    "deprecated": route.deprecated,
                    "operation_id": route.operation_id,
                    "include_in_schema": getattr(route, "include_in_schema", True),
                    "openapi_extra": getattr(route, "openapi_extra", {}),
                    "security": getattr(route, "security", None),
                    "callbacks": getattr(route, "callbacks", []),
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

        # Mounted sub-applications
        for mount_path, mounted_app, _name in self._mounts:
            if isinstance(mounted_app, FastAPI):
                # Collect routes from the mounted FastAPI app with prefix
                sub_routes = mounted_app._collect_all_routes()
                for r in sub_routes:
                    original = r["path"]
                    r["path"] = mount_path.rstrip("/") + ("" if original == "/" else original)
                    if not r["path"]:
                        r["path"] = "/"
                all_routes.extend(sub_routes)
            elif isinstance(mounted_app, APIRouter):
                all_routes.extend(
                    self._collect_routes_from_router(mounted_app, prefix=mount_path)
                )

        return all_routes

    # ------------------------------------------------------------------
    # URL building
    # ------------------------------------------------------------------

    def url_path_for(self, name: str, /, **path_params: Any) -> "URLPath":
        """Return the URL path for a named route, filling in path_params.

        Matches Starlette/FastAPI's behavior: looks up routes by their `name`
        (endpoint function name by default) and substitutes {param}
        placeholders.  Prepends root_path if configured.

        Returns a URLPath (str subclass) matching Starlette's return type,
        so callers can use `.make_absolute_url(base_url=...)`.
        """
        from urllib.parse import quote

        for route in self._collect_all_routes():
            if route.get("handler_name") == name:
                path = route["path"]
                import re

                def _sub(match: re.Match) -> str:
                    pname = match.group(1).split(":")[0]
                    if pname not in path_params:
                        raise KeyError(f"Missing path param {pname!r} for route {name!r}")
                    val = path_params[pname]
                    if ":path" in match.group(0):
                        return str(val)
                    return quote(str(val), safe="")

                filled = re.sub(r"\{([^}]+)\}", _sub, path)
                root = getattr(self, "root_path", "") or ""
                full = root.rstrip("/") + filled if root else filled
                return URLPath(full)

        raise LookupError(f"No route named {name!r}")

    # ------------------------------------------------------------------
    # OpenAPI schema
    # ------------------------------------------------------------------

    def openapi(self) -> dict[str, Any]:
        """Return the OpenAPI schema dict (cached after first call)."""
        if not hasattr(self, "_openapi_schema"):
            route_dicts = self._collect_all_routes()
            # Add root_path to servers if configured (matches run_server() behavior)
            effective_servers = self.servers
            if self.root_path and self.root_path_in_servers and not effective_servers:
                effective_servers = [{"url": self.root_path}]
            self._openapi_schema = generate_openapi_schema(
                title=self.title,
                version=self.version,
                description=self.description,
                routes=route_dicts,
                servers=effective_servers,
                terms_of_service=self.terms_of_service,
                contact=self.contact,
                license_info=self.license_info,
                openapi_tags=self.openapi_tags,
            )
        return self._openapi_schema

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    def _run_startup_handlers(self) -> None:
        """Execute all registered startup handlers (P0 fix #2)."""
        for handler in self._on_startup:
            if asyncio.iscoroutinefunction(handler):
                asyncio.run(handler())
            else:
                handler()

    def _run_shutdown_handlers(self) -> None:
        """Execute all registered shutdown handlers (P0 fix #2)."""
        for handler in self._on_shutdown:
            if asyncio.iscoroutinefunction(handler):
                asyncio.run(handler())
            else:
                handler()

    def _run_lifespan_startup(self) -> None:
        """Run the lifespan context manager startup phase (P0 fix #3).

        Enters the lifespan async context manager, stores any yielded state
        on app.state, and saves the context manager for cleanup at shutdown.
        """
        if not self.lifespan:
            return

        lifespan_cm = self.lifespan(self)
        self._lifespan_cm = lifespan_cm

        async def _enter_lifespan():
            state = await lifespan_cm.__aenter__()
            if state:
                for k, v in state.items():
                    setattr(self.state, k, v)

        asyncio.run(_enter_lifespan())

    def _run_lifespan_shutdown(self) -> None:
        """Run the lifespan context manager shutdown phase (P0 fix #3)."""
        if not hasattr(self, "_lifespan_cm") or self._lifespan_cm is None:
            return

        async def _exit_lifespan():
            await self._lifespan_cm.__aexit__(None, None, None)

        asyncio.run(_exit_lifespan())

    # ------------------------------------------------------------------
    # Server launch
    # ------------------------------------------------------------------

    def run(self, host: str = "127.0.0.1", port: int = 8000, **kwargs: Any) -> None:
        """Collect routes, hand them to the Rust core, and start serving."""
        from fastapi_rs._fastapi_rs_core import ParamInfo, RouteInfo, run_server

        # Run lifespan startup phase if lifespan is set (P0 fix #3)
        if self.lifespan:
            self._run_lifespan_startup()
            # Register lifespan shutdown via atexit
            atexit.register(self._run_lifespan_shutdown)

        # Run startup event handlers (P0 fix #2)
        self._run_startup_handlers()

        # Register shutdown handlers via atexit (P0 fix #2)
        if self._on_shutdown:
            atexit.register(self._run_shutdown_handlers)

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
            # Auto-add root_path to servers if configured
            effective_servers = self.servers
            if self.root_path and self.root_path_in_servers and not effective_servers:
                effective_servers = [{"url": self.root_path}]
            openapi_schema = generate_openapi_schema(
                title=self.title,
                version=self.version,
                description=self.description,
                routes=http_routes,
                servers=effective_servers,
                terms_of_service=self.terms_of_service,
                contact=self.contact,
                license_info=self.license_info,
                openapi_tags=self.openapi_tags,
            )
            openapi_json = json.dumps(openapi_schema)

        middleware_config = self._build_middleware_config()

        # Collect static file mounts for Rust-side ServeDir
        static_mounts = []
        for mount_path, mounted_app, _name in self._mounts:
            if hasattr(mounted_app, 'directory') and mounted_app.directory:
                static_mounts.append((mount_path, str(mounted_app.directory)))

        run_server(
            route_infos,
            host,
            port,
            middleware_config,
            openapi_json,
            self.docs_url,
            self.redoc_url,
            self.openapi_url,
            static_mounts,
            self.root_path or None,
        )
