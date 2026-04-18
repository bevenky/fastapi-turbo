"""The main FastAPI-compatible application class."""

from __future__ import annotations

import asyncio
import atexit
import inspect
import json
from typing import Any, Callable, Sequence

from fastapi_rs._introspect import introspect_endpoint
from fastapi_rs._openapi import generate_openapi_schema
from fastapi_rs._resolution import build_resolution_plan, _make_sync_wrapper
from fastapi_rs.datastructures import State
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
    by_alias=True,
):
    """Filter a handler result through a response_model Pydantic class.

    by_alias=True (FastAPI default) — honor Pydantic Field(alias=...) and
    Field(serialization_alias=...) in output. Critical for APIs that use
    aliased fields (camelCase over snake_case, etc.).
    """
    if response_model is None or result is None:
        return result

    has_filters = (
        include is not None
        or exclude is not None
        or exclude_unset
        or exclude_defaults
        or exclude_none
        or by_alias is False
    )

    try:
        # Always go through model_validate + model_dump when by_alias matters —
        # we can't take the fast "strip extra keys" path because field names
        # differ from aliases.
        fast_path_ok = not has_filters and not _model_has_aliases(response_model)

        import dataclasses as _dc
        if isinstance(result, dict):
            if fast_path_ok:
                model_fields = response_model.model_fields
                return {k: v for k, v in result.items() if k in model_fields}
            validated = response_model.model_validate(result)
        elif hasattr(result, "model_dump"):
            if fast_path_ok and type(result) is response_model:
                return result.model_dump()
            # If `result` is ALREADY an instance of response_model, use it
            # directly — round-tripping via model_dump()+model_validate()
            # would mark ALL fields as explicitly set, defeating
            # exclude_unset / exclude_defaults.
            if type(result) is response_model:
                validated = result
            else:
                validated = response_model.model_validate(
                    result.model_dump() if hasattr(result, "model_dump") else result
                )
        elif _dc.is_dataclass(result) and not isinstance(result, type):
            # Convert dataclass → dict so Pydantic model_validate can apply
            # exclude_none / exclude_unset / aliases consistently.
            as_dict = _dc.asdict(result)
            if response_model is type(result) or not hasattr(response_model, "model_validate"):
                # Same dataclass — apply exclude options manually.
                if exclude_none:
                    as_dict = {k: v for k, v in as_dict.items() if v is not None}
                return as_dict
            validated = response_model.model_validate(as_dict)
        else:
            return result

        dump_kwargs = {"by_alias": by_alias}
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


def _maybe_print_debug_traceback(app, exc):
    """When app.debug is True, print the full traceback to stderr before
    the exception is routed to a handler. Matches FastAPI's ``debug=True``
    developer-ergonomics behavior.

    HTTPException is never traceback-printed — those are normal control flow.
    """
    if app is None or not getattr(app, "debug", False):
        return
    try:
        from fastapi_rs.exceptions import HTTPException
        if isinstance(exc, HTTPException):
            return
    except Exception:
        pass
    import sys, traceback
    traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)


def _model_needs_full_dump(model_cls) -> bool:
    """True if the model has aliases, computed fields, or custom serializers.

    When any of these are present, the 'strip extra keys' fast path is unsafe —
    we must go through model_validate + model_dump so Pydantic can:
      - rename fields via alias / serialization_alias
      - include computed_field properties in output
      - run field_serializer / model_serializer hooks

    Cached per class on the class itself — paid once per model, not per request.
    """
    cached = getattr(model_cls, "__fastapi_rs_needs_full_dump__", None)
    if cached is not None:
        return cached
    needs = False
    # Field-level aliases
    fields = getattr(model_cls, "model_fields", None) or {}
    for finfo in fields.values():
        if getattr(finfo, "alias", None) is not None:
            needs = True
            break
        if getattr(finfo, "serialization_alias", None) is not None:
            needs = True
            break
    # Computed fields (Pydantic v2 @computed_field)
    if not needs:
        computed = getattr(model_cls, "model_computed_fields", None)
        if computed:
            needs = True
    # Custom serializers (field_serializer / model_serializer) — Pydantic v2
    # stores these in __pydantic_decorators__.
    if not needs:
        decorators = getattr(model_cls, "__pydantic_decorators__", None)
        if decorators is not None:
            if getattr(decorators, "field_serializers", None):
                needs = True
            elif getattr(decorators, "model_serializers", None):
                needs = True
    try:
        model_cls.__fastapi_rs_needs_full_dump__ = needs
    except Exception:
        pass
    return needs


# Back-compat alias for existing callers
_model_has_aliases = _model_needs_full_dump


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


def _apply_status_code(result, status_code: int):
    """Apply a route-declared `status_code=N` to the result.

    If the handler already returned a Response (or a Response-like), we
    set its status_code directly. Otherwise, wrap the bare return value
    in a JSONResponse with the declared status.
    """
    if result is None:
        # None + declared status_code → empty response with that status.
        from fastapi_rs.responses import Response as _R
        return _R(content=b"", status_code=status_code)
    if hasattr(result, "status_code") and hasattr(result, "body"):
        # Only override if handler didn't explicitly set a non-200 code.
        try:
            current = int(result.status_code)
            if current == 200:
                result.status_code = status_code
        except Exception:
            pass
        return result
    # Bare dict/list/str → wrap as JSONResponse with the declared status.
    from fastapi_rs.responses import JSONResponse as _J
    return _J(content=result, status_code=status_code)


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
    response_model_by_alias=True,
    response_class=None,
    status_code=None,
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
    _rm_by_alias = response_model_by_alias
    _response_class = response_class

    dep_steps = [p for p in params if p["kind"] == "dependency"]
    _has_exc_handlers = app is not None and bool(getattr(app, "exception_handlers", None))
    _debug_on = app is not None and bool(getattr(app, "debug", False))
    _has_enum_params = any(
        p.get("enum_class") is not None and p.get("_is_handler_param")
        for p in params
    )
    if not dep_steps:
        if (
            response_model is not None
            or _response_class is not None
            or _has_exc_handlers
            or _debug_on
            or status_code is not None
            or _has_enum_params
        ):
            # Even without deps, we may need response_model filtering or response_class wrapping
            handler_param_names = {p["name"] for p in params if p.get("_is_handler_param")}
            # Build enum coercion map: {param_name: EnumClass} for query/path params
            _enum_coerce = {
                p["name"]: p["enum_class"]
                for p in params
                if p.get("enum_class") is not None and p.get("_is_handler_param")
            }
            handler_func = endpoint
            if inspect.iscoroutinefunction(handler_func):
                handler_func = _make_sync_wrapper(handler_func)

            _app_ref = app

            def _compiled_no_deps(**kwargs):
                try:
                    filtered = {k: kwargs[k] for k in handler_param_names if k in kwargs}
                    # Coerce raw strings to Enum types (FastAPI does this automatically)
                    for _ek, _ecls in _enum_coerce.items():
                        if _ek in filtered and isinstance(filtered[_ek], str):
                            try:
                                filtered[_ek] = _ecls(filtered[_ek])
                            except (ValueError, KeyError):
                                pass
                    result = handler_func(**filtered)
                except Exception as exc:
                    _maybe_print_debug_traceback(_app_ref, exc)
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
                        by_alias=_rm_by_alias,
                    )
                if _response_class is not None:
                    result = _wrap_response_class(result, _response_class)
                if status_code is not None:
                    result = _apply_status_code(result, status_code)
                return result

            return _compiled_no_deps
        return None

    handler_param_names = {p["name"] for p in params if p.get("_is_handler_param")}

    # Build enum coercion map for the deps path too
    _enum_coerce_deps = {
        p["name"]: p["enum_class"]
        for p in params
        if p.get("enum_class") is not None and p.get("_is_handler_param")
    }

    # Prepare dep callables (wrap async -> sync) and store originals for override lookup
    dep_chain = []
    for dep in dep_steps:
        original_func = dep.get("_original_dep_callable", dep["dep_callable"])
        func = dep["dep_callable"]
        is_generator = dep.get("is_generator_dep", False)
        if inspect.iscoroutinefunction(func) and not is_generator:
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
    if inspect.iscoroutinefunction(handler_func):
        handler_func = _make_sync_wrapper(handler_func)

    # Capture app reference for override lookup at call time
    _app = app

    def _compiled(**kwargs):
        resolved = kwargs
        cache = {}
        generators_to_cleanup = []

        try:
            for name, func, original_func, input_map, func_id, is_generator in dep_chain:
                # Check dependency_overrides at call time (P0 fix #1)
                actual_func = func
                if _app is not None and _app.dependency_overrides:
                    override = _app.dependency_overrides.get(original_func)
                    if override is not None:
                        actual_func = override
                        if inspect.iscoroutinefunction(actual_func):
                            actual_func = _make_sync_wrapper(actual_func)

                if func_id is not None and func_id in cache:
                    resolved[name] = cache[func_id]
                    continue
                dk = {pn: resolved[sk] for pn, sk in input_map if sk in resolved}

                if is_generator:
                    # Generator dep (yield) support — sync generators drive via
                    # next(); async generators via anext() on a one-shot loop so
                    # we stay on the same thread as the rest of the handler.
                    gen = actual_func(**dk)
                    if inspect.isasyncgen(gen):
                        import asyncio as _asyncio
                        loop = _asyncio.new_event_loop()
                        try:
                            result = loop.run_until_complete(gen.__anext__())
                        finally:
                            # Keep the generator for cleanup AFTER the handler.
                            # Don't close the loop — we'll reuse it for cleanup.
                            pass
                        generators_to_cleanup.append((gen, loop))
                    else:
                        result = next(gen)
                        generators_to_cleanup.append((gen, None))
                else:
                    result = actual_func(**dk)

                resolved[name] = result
                if func_id is not None:
                    cache[func_id] = result
        except Exception as dep_exc:
            # Dependency raised — route through exception_handlers like
            # FastAPI/Starlette does. SGLang depends on this (route-level
            # `dependencies=[Depends(...)]` that raise HTTPException).
            _maybe_print_debug_traceback(_app, dep_exc)
            if _app is not None and _app.exception_handlers:
                handler_result = _app._invoke_exception_handler(dep_exc)
                if handler_result is not None:
                    return handler_result
            raise

        try:
            try:
                _hkwargs = {k: resolved[k] for k in handler_param_names if k in resolved}
                # Coerce raw strings to Enum types (FastAPI does this automatically)
                for _ek, _ecls in _enum_coerce_deps.items():
                    if _ek in _hkwargs and isinstance(_hkwargs[_ek], str):
                        try:
                            _hkwargs[_ek] = _ecls(_hkwargs[_ek])
                        except (ValueError, KeyError):
                            pass
                result = handler_func(**_hkwargs)
            except Exception as exc:
                # In debug mode, surface the full traceback on non-HTTPException errors.
                _maybe_print_debug_traceback(_app, exc)
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
            if status_code is not None:
                result = _apply_status_code(result, status_code)
            return result
        finally:
            # Clean up generator deps in reverse order — sync via next(),
            # async via anext() on the one-shot loop captured earlier.
            for gen, loop in reversed(generators_to_cleanup):
                try:
                    if loop is not None:
                        try:
                            loop.run_until_complete(gen.__anext__())
                        except StopAsyncIteration:
                            pass
                        finally:
                            loop.close()
                    else:
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

    is_async_endpoint = inspect.iscoroutinefunction(endpoint)

    # Shared scope — recycled per request (shallow copy cheap)
    def _make_scope(kwargs):
        return {
            "type": "http",
            "app": app,
            "method": kwargs.pop("_request_method", "GET"),
            "path": kwargs.pop("_request_path", "/"),
            "query_string": kwargs.pop("_request_query", "").encode(),
            "headers": kwargs.pop("_request_headers", []),
            "_handler_kwargs": kwargs,
        }

    def _call_handler_sync(kwargs):
        """Run the underlying handler, returning a Response-normalized value."""
        # Replace any Rust-injected Request objects with the middleware's
        # Request so that request.state set by middleware propagates to the
        # handler. The middleware Request shares state with the middleware chain.
        mw_request = kwargs.pop("_middleware_request", None)
        if mw_request is not None:
            for key in list(kwargs.keys()):
                val = kwargs.get(key)
                if isinstance(val, _Request):
                    # Merge scope data from Rust's Request (has body, path_params,
                    # app, etc.) into the middleware's Request.
                    for sk, sv in val._scope.items():
                        if sk not in mw_request._scope:
                            mw_request._scope[sk] = sv
                    # Copy over the state from middleware's Request
                    kwargs[key] = mw_request
                    break
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

            # Detect async callable: either a bare async def, or a class
            # instance with async __call__ (e.g., SessionMiddleware).
            is_async_mw = (
                inspect.iscoroutinefunction(mw)
                or inspect.iscoroutinefunction(getattr(mw, "__call__", None))
            )
            if is_async_mw:
                coro = mw(request, call_next)
                try:
                    coro.send(None)
                    # Middleware suspended on real I/O (e.g., async DB call).
                    # Fall back to the full event-loop path.
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
        # Store the middleware's Request object in kwargs so Rust's
        # inject_framework_objects can reuse it instead of creating a new one.
        # This ensures request.state set by middleware propagates to the handler.
        kwargs["_middleware_request"] = request
        try:
            return runner(request, kwargs)
        except _MiddlewareSuspendedError:
            # Fallback: drive everything through a fresh event loop
            return _drive_async_fallback(endpoint, middlewares, app, kwargs, is_async_endpoint)

    wrapped_sync._has_http_middleware = True
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

            if inspect.iscoroutinefunction(mw):
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
        summary: str | None = None,
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
        on_startup: Sequence[Callable] | None = None,
        on_shutdown: Sequence[Callable] | None = None,
        dependencies: Sequence | None = None,
        root_path: str = "",
        root_path_in_servers: bool = True,
        exception_handlers: dict | None = None,
        default_response_class: Any = None,
        responses: dict | None = None,
        debug: bool = False,
        redirect_slashes: bool = True,
        max_request_size: int | None = None,
        webhooks: "APIRouter | None" = None,
        external_docs: dict[str, Any] | None = None,
        middleware: Sequence | None = None,
        swagger_ui_oauth2_redirect_url: str | None = "/docs/oauth2-redirect",
        swagger_ui_init_oauth: dict | None = None,
        swagger_ui_parameters: dict | None = None,
        generate_unique_id_function: Callable | None = None,
        separate_input_output_schemas: bool = True,
        callbacks: list | None = None,
        deprecated: bool | None = None,
        include_in_schema: bool = True,
        openapi_prefix: str = "",
        strict_content_type: bool = True,
        **kwargs: Any,
    ):
        self.title = title
        self.summary = summary
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
        # Handle deprecated openapi_prefix -> root_path alias (Gap 20)
        if openapi_prefix and not root_path:
            import warnings
            warnings.warn(
                "openapi_prefix has been deprecated in favor of root_path, "
                "which follows more closely the ASGI spec.",
                DeprecationWarning,
                stacklevel=2,
            )
            root_path = openapi_prefix
        self.openapi_prefix = openapi_prefix
        self.root_path = root_path
        self.root_path_in_servers = root_path_in_servers
        self.generate_unique_id_function = generate_unique_id_function
        self.separate_input_output_schemas = separate_input_output_schemas
        self.callbacks = callbacks or []
        self.deprecated = deprecated
        self.include_in_schema = include_in_schema
        self.strict_content_type = strict_content_type
        # Map of exception class (or int status code) -> handler callable
        self.exception_handlers: dict = dict(exception_handlers or {})
        # Default response class applied app-wide when routes/routers don't override
        self.default_response_class = default_response_class
        # App-level default responses merged into every route's OpenAPI entry
        self.responses: dict = dict(responses or {})
        # When True, 500 responses include Python traceback (dev only)
        self.debug: bool = bool(debug)
        # When True (default), a request for /foo/ with a route /foo defined
        # (or vice-versa) is redirected with 307 to the canonical path.
        # Matches Starlette's `redirect_slashes` behaviour.
        self.redirect_slashes: bool = bool(redirect_slashes)
        # Max request body size in bytes. 413 Payload Too Large beyond this.
        self.max_request_size: int | None = max_request_size
        # OpenAPI webhooks — mirrors `app.webhooks` in FastAPI. Use as a
        # router-like container for webhook definitions that appear under
        # the top-level `webhooks` field of the OpenAPI schema.
        self.webhooks: APIRouter = webhooks if webhooks is not None else APIRouter()
        # Top-level OpenAPI externalDocs — accept both our `external_docs`
        # and FastAPI's `openapi_external_docs` spelling.
        if external_docs is None and "openapi_external_docs" in kwargs:
            external_docs = kwargs.pop("openapi_external_docs")
        self.external_docs: dict[str, Any] | None = external_docs

        self.router = APIRouter()
        self.state = State()
        self.dependency_overrides: dict[Callable, Callable] = {}
        self.dependencies: list = list(dependencies or [])

        self._middleware_stack: list[tuple[type, dict[str, Any]]] = []
        # @app.middleware("http") registered middlewares — Python-side HTTP middlewares
        # that wrap each user route handler.
        self._http_middlewares: list[Callable] = []
        self._on_startup: list[Callable] = []
        self._on_shutdown: list[Callable] = []
        self._included_routers: list[tuple[APIRouter, str, list[str], dict]] = []
        self._mounts: list[tuple[str, Any, str | None]] = []

        # Swagger UI customization params
        self.swagger_ui_oauth2_redirect_url = swagger_ui_oauth2_redirect_url
        self.swagger_ui_init_oauth = swagger_ui_init_oauth
        self.swagger_ui_parameters = swagger_ui_parameters

        # on_startup / on_shutdown lists passed via __init__ (Gap 9)
        if on_startup:
            self._on_startup.extend(on_startup)
        if on_shutdown:
            self._on_shutdown.extend(on_shutdown)

        # middleware= list passed via __init__ (Gap 10)
        # Each element is a Middleware(cls, **options) namedtuple-like from starlette.
        if middleware:
            for m in middleware:
                cls = m.cls if hasattr(m, "cls") else m[0]
                kwargs_m = m.kwargs if hasattr(m, "kwargs") else (m[1] if len(m) > 1 else {})
                self.add_middleware(cls, **kwargs_m)

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

    def api_route(self, path: str, **kwargs: Any):
        return self.router.api_route(path, **kwargs)

    # ------------------------------------------------------------------
    # WebSocket decorator
    # ------------------------------------------------------------------

    def websocket(self, path: str, **kwargs: Any):
        return self.router.websocket(path, **kwargs)

    # ------------------------------------------------------------------
    # Imperative route registration
    # ------------------------------------------------------------------

    def add_api_route(self, path: str, endpoint: Callable, **kwargs: Any) -> None:
        """Imperative form of @app.get / @app.post / etc."""
        return self.router.add_api_route(path, endpoint, **kwargs)

    def add_api_websocket_route(self, path: str, endpoint: Callable, **kwargs: Any) -> None:
        """Imperative form of @app.websocket."""
        return self.router.add_websocket_route(path, endpoint, **kwargs)

    def add_route(self, path: str, route: Callable, **kwargs: Any) -> None:
        """Starlette-compatible add_route (delegates to add_api_route)."""
        return self.router.add_api_route(path, route, **kwargs)

    def websocket_route(self, path: str, name: str | None = None, **kwargs: Any):
        """Decorator to register a WebSocket route (delegates to router)."""
        return self.router.websocket_route(path, name=name, **kwargs)

    # ------------------------------------------------------------------
    # Stubs for FastAPI compatibility
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """No-op stub for Starlette compatibility."""
        pass

    def build_middleware_stack(self):
        """No-op stub for Starlette compatibility."""
        return self

    def host(self, hostname: str, app: Any = None, name: str | None = None) -> None:
        """Store host-based routing info (stub for Starlette compatibility)."""
        if not hasattr(self, "_hosts"):
            self._hosts: list[tuple[str, Any, str | None]] = []
        self._hosts.append((hostname, app, name))

    # ------------------------------------------------------------------
    # Routes property
    # ------------------------------------------------------------------

    @property
    def routes(self) -> list:
        """Return all collected route objects (Starlette/FastAPI compatibility)."""
        all_routes = list(self.router.routes)
        for router, _prefix, _tags, _meta in self._included_routers:
            all_routes.extend(router.routes)
        return all_routes

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
        dependencies: Sequence | None = None,
        responses: dict | None = None,
        deprecated: bool | None = None,
        include_in_schema: bool = True,
        default_response_class: Any = None,
        callbacks: list | None = None,
        generate_unique_id_function: Callable | None = None,
    ) -> None:
        """Register a child router for later flattening."""
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

    # ------------------------------------------------------------------
    # Middleware
    # ------------------------------------------------------------------

    def add_middleware(self, middleware_cls, **kwargs: Any) -> None:
        """Register a middleware class.

        Handles three cases:
        1. Known Rust/Tower middleware (CORS, GZip, etc.) → Rust stack
        2. Python HTTP middleware (our marker) → per-handler chain
        3. BaseHTTPMiddleware subclass (Qwen pattern) → converted to
           @app.middleware("http") callable via its dispatch() method
        """
        mw_type = getattr(middleware_cls, "_fastapi_rs_middleware_type", None)
        if mw_type and mw_type.startswith("python_http_"):
            try:
                instance = middleware_cls(app=self, **kwargs)
            except TypeError:
                instance = middleware_cls(**kwargs)
            self._http_middlewares.append(instance)
            return

        # BaseHTTPMiddleware subclass — Qwen uses this for auth middleware.
        # Convert to an @app.middleware("http") function by wrapping dispatch().
        from fastapi_rs.middleware.base import BaseHTTPMiddleware
        if isinstance(middleware_cls, type) and issubclass(middleware_cls, BaseHTTPMiddleware):
            try:
                instance = middleware_cls(app=self, **kwargs)
            except TypeError:
                instance = middleware_cls(**kwargs)

            async def _dispatch_wrapper(request, call_next, _inst=instance):
                return await _inst.dispatch(request, call_next)

            self._http_middlewares.append(_dispatch_wrapper)
            return

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
            if inspect.iscoroutinefunction(handler):
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

    def _get_all_dependencies_for_route(
        self, router: APIRouter, route, include_deps: list | None = None,
    ) -> list:
        """Merge app-level, include-level, router-level, and route-level dependencies."""
        merged = []
        # App-level dependencies first
        merged.extend(self.dependencies)
        # include_router()-level dependencies (between app and router)
        if include_deps:
            merged.extend(include_deps)
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
        include_deps: list | None = None,
        include_responses: dict | None = None,
        include_deprecated: bool | None = None,
        include_in_schema: bool = True,
        include_default_response_class: Any = None,
    ) -> list[dict[str, Any]]:
        """Recursively flatten a router tree into a list of route dicts."""
        extra_tags = extra_tags or []
        include_deps = include_deps or []
        include_responses = include_responses or {}
        collected: list[dict[str, Any]] = []

        full_prefix = prefix + router.prefix

        # Merge the router's own tags into extra_tags so all routes
        # within this router inherit them (FastAPI parity).
        if router.tags:
            extra_tags = extra_tags + router.tags

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

            # Merge global/include/router-level/route-level dependencies
            merged_deps = self._get_all_dependencies_for_route(router, route, include_deps=include_deps)

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
            rm_by_alias = getattr(route, "response_model_by_alias", True)
            response_class = getattr(route, "response_class", None)
            # Cascade default_response_class: route → router → include-level → app
            if response_class is None:
                response_class = getattr(router, "default_response_class", None)
            if response_class is None and include_default_response_class is not None:
                response_class = include_default_response_class
            if response_class is None:
                response_class = getattr(self, "default_response_class", None)

            rm_kwargs = dict(
                response_model_include=rm_include,
                response_model_exclude=rm_exclude,
                response_model_exclude_unset=rm_exclude_unset,
                response_model_exclude_defaults=rm_exclude_defaults,
                response_model_exclude_none=rm_exclude_none,
                response_model_by_alias=rm_by_alias,
                response_class=response_class,
            )

            # KEY OPTIMIZATION: Compile deps + handler into a SINGLE Python function.
            # This reduces N+1 PyO3 boundary crossings to just 1.
            # Rust calls one function with extracted params -> gets back the response.
            if has_deps:
                compiled = _try_compile_handler(
                    endpoint, params, app=self, response_model=response_model,
                    status_code=route.status_code,
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
            elif (
                response_model is not None
                or response_class is not None
                or route.status_code
                or self.exception_handlers
                or self.debug
                or any(p.get("enum_class") is not None for p in params)
            ):
                # No deps but has response_model/response_class/status_code/
                # exception_handlers/enum params — wrap handler via compile.
                compiled = _try_compile_handler(
                    endpoint, params, app=self, response_model=response_model,
                    status_code=route.status_code,
                    **rm_kwargs,
                )
                if compiled is not None:
                    endpoint = compiled
                    is_async = False

            # Wrap handler when multiple body params are combined.
            # CRITICAL ORDERING:
            #   - Rust sends kwargs containing `_combined_body` (never `item`,
            #     `user` etc — the individual body params were removed from
            #     introspection).
            #   - The unwrap wrapper MUST run BEFORE the compiled handler so
            #     that by the time the compiled handler receives kwargs, the
            #     `_combined_body` has been split back into original body
            #     names. However, the compiled handler's own filtering uses
            #     handler_param_names which don't include the original body
            #     names either.
            #   - Simplest correct flow: unwrap wraps the USER endpoint
            #     directly, NOT the compiled endpoint. And the unwrap gets
            #     kwargs from Rust (which include `_combined_body` + non-body
            #     params like query/path). It splits and calls user handler
            #     with the original names.
            #   - Then _compiled_no_deps (if any) wraps the unwrap, but its
            #     filter check is satisfied because `_combined_body` is now
            #     in handler_param_names (we marked _is_handler_param=True).
            combined = [p for p in params if p.get("name") == "_combined_body" and p.get("_body_param_names")]
            if combined:
                body_param_names = combined[0]["_body_param_names"]
                # Use the ORIGINAL user endpoint, not the compiled one — we
                # unwrap the body first, then call the user's real function.
                user_endpoint = route.endpoint
                user_is_async = inspect.iscoroutinefunction(user_endpoint)

                if user_is_async:
                    async def _unwrap_combined_async(
                        _body_names=body_param_names,
                        _orig=user_endpoint,
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
                        _orig=user_endpoint,
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
                    # Merge: app defaults → include-level → router defaults → route (route wins)
                    "responses": {
                        **self.responses,
                        **include_responses,
                        **getattr(router, "responses", {}),
                        **getattr(route, "responses", {}),
                    },
                    "response_model": response_model,
                    "deprecated": route.deprecated or bool(include_deprecated),
                    "operation_id": route.operation_id,
                    "include_in_schema": (
                        getattr(route, "include_in_schema", True) and include_in_schema
                    ),
                    "openapi_extra": getattr(route, "openapi_extra", {}),
                    "security": getattr(route, "security", None),
                    "callbacks": getattr(route, "callbacks", []),
                    "servers": getattr(route, "servers", None),
                    "external_docs": getattr(route, "external_docs", None),
                }
            )

        # Recurse into child routers
        for child_router, child_prefix, child_tags, child_meta in router._included_routers:
            collected.extend(
                self._collect_routes_from_router(
                    child_router,
                    prefix=full_prefix + child_prefix,
                    extra_tags=extra_tags + child_tags,
                    include_deps=child_meta.get("dependencies", []),
                    include_responses=child_meta.get("responses", {}),
                    include_deprecated=child_meta.get("deprecated"),
                    include_in_schema=child_meta.get("include_in_schema", True),
                    include_default_response_class=child_meta.get("default_response_class"),
                )
            )

        return collected

    def _collect_all_routes(self) -> list[dict[str, Any]]:
        """Walk the root router and all included routers, returning a flat list."""
        # Routes registered directly on self.router
        all_routes = self._collect_routes_from_router(self.router)

        # Routers added via app.include_router(...)
        for router, prefix, tags, meta in self._included_routers:
            all_routes.extend(
                self._collect_routes_from_router(
                    router,
                    prefix=prefix,
                    extra_tags=tags,
                    include_deps=meta.get("dependencies", []),
                    include_responses=meta.get("responses", {}),
                    include_deprecated=meta.get("deprecated"),
                    include_in_schema=meta.get("include_in_schema", True),
                    include_default_response_class=meta.get("default_response_class"),
                )
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
            webhook_dicts = self._collect_routes_from_router(self.webhooks)
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
                webhooks=webhook_dicts,
                external_docs=self.external_docs,
                summary=self.summary,
                separate_input_output_schemas=self.separate_input_output_schemas,
            )
        return self._openapi_schema

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    def _run_startup_handlers(self) -> None:
        """Execute all registered startup handlers (P0 fix #2)."""
        for handler in self._on_startup:
            if inspect.iscoroutinefunction(handler):
                asyncio.run(handler())
            else:
                handler()

    def _run_shutdown_handlers(self) -> None:
        """Execute all registered shutdown handlers (P0 fix #2)."""
        for handler in self._on_shutdown:
            if inspect.iscoroutinefunction(handler):
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
                    has_default=p.get("has_default", False),
                    model_class=p.get("model_class"),
                    alias=p.get("alias"),
                    dep_callable=p.get("dep_callable"),
                    dep_callable_id=p.get("dep_callable_id"),
                    is_async_dep=p.get("is_async_dep", False),
                    is_generator_dep=p.get("is_generator_dep", False),
                    dep_input_names=p.get("dep_input_map", []),
                    is_handler_param=p.get("_is_handler_param", True),
                    scalar_validator=p.get("scalar_validator"),
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
            webhook_dicts = self._collect_routes_from_router(self.webhooks)
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
                webhooks=webhook_dicts,
                external_docs=self.external_docs,
                summary=self.summary,
                separate_input_output_schemas=self.separate_input_output_schemas,
            )
            openapi_json = json.dumps(openapi_schema)

        middleware_config = self._build_middleware_config()

        # Collect static file mounts for Rust-side ServeDir
        static_mounts = []
        for mount_path, mounted_app, _name in self._mounts:
            if hasattr(mounted_app, 'directory') and mounted_app.directory:
                static_mounts.append((mount_path, str(mounted_app.directory)))

        # Build a tiny not_found_handler callable the Rust 404 fallback
        # can invoke: takes (method, path), returns (status, body_bytes).
        # Dispatches to whatever the user registered with
        # ``@app.exception_handler(404)`` (or HTTPException class).
        not_found_handler = None
        from fastapi_rs.exceptions import HTTPException as _HTTPExc
        _app_self = self

        def _rust_404_handler(method: str, path: str):
            handler = _app_self.exception_handlers.get(404)
            if handler is None:
                handler = _app_self.exception_handlers.get(_HTTPExc)
            if handler is None:
                # No custom handler — let Rust emit the default body.
                return (404, b'{"detail":"Not Found"}')
            # Build a minimal Request
            from fastapi_rs.requests import Request
            req = Request({
                "type": "http",
                "method": method,
                "path": path,
                "headers": [],
                "query_string": b"",
                "path_params": {},
            })
            exc = _HTTPExc(status_code=404, detail="Not Found")
            result = handler(req, exc)
            # Drive coroutine if returned
            if inspect.iscoroutine(result):
                import asyncio as _asyncio
                loop = _asyncio.new_event_loop()
                try:
                    result = loop.run_until_complete(result)
                finally:
                    loop.close()
            # Extract (status, body)
            status = getattr(result, "status_code", 404)
            body = getattr(result, "body", None)
            if body is None:
                import json as _json
                body = _json.dumps({"detail": "Not Found"}).encode()
            elif isinstance(body, str):
                body = body.encode("utf-8")
            return (int(status), bytes(body))

        if self.exception_handlers.get(404) is not None or self.exception_handlers.get(_HTTPExc) is not None:
            not_found_handler = _rust_404_handler

        # Rust-side validation dispatcher: when the user registered
        # @exception_handler(RequestValidationError), let the Rust validation
        # error paths route the detail through it.
        validation_handler = None
        from fastapi_rs.exceptions import RequestValidationError as _RVE
        if _RVE in self.exception_handlers:
            from fastapi_rs.requests import Request as _Req
            import json as _json
            _user_handler = self.exception_handlers[_RVE]

            def _rust_validation_handler(detail_json):
                """Called from Rust on validation failure.

                detail_json is the pre-built FastAPI-style 422 detail list
                (``{"detail": [...]}``) as a JSON string.
                """
                if isinstance(detail_json, (bytes, bytearray)):
                    detail_json = bytes(detail_json).decode()
                try:
                    detail_obj = _json.loads(detail_json)
                except Exception:
                    detail_obj = {"detail": detail_json}
                errors_list = detail_obj.get("detail", [])
                exc = _RVE(errors_list)
                req = _Req({
                    "type": "http",
                    "method": "POST",
                    "path": "/",
                    "headers": [],
                    "query_string": b"",
                })
                res = _user_handler(req, exc)
                if inspect.iscoroutine(res):
                    import asyncio as _asyncio
                    loop = _asyncio.new_event_loop()
                    try:
                        res = loop.run_until_complete(res)
                    finally:
                        loop.close()
                status = int(getattr(res, "status_code", 422) or 422)
                body = getattr(res, "body", None)
                if body is None:
                    content = getattr(res, "content", None)
                    if content is None:
                        body = _json.dumps(detail_obj).encode()
                    elif isinstance(content, (bytes, bytearray)):
                        body = bytes(content)
                    elif isinstance(content, str):
                        body = content.encode()
                    else:
                        body = _json.dumps(content).encode()
                elif isinstance(body, str):
                    body = body.encode()
                # Pull media_type from the response; default to json
                ct = getattr(res, "media_type", None) or "application/json"
                headers = getattr(res, "headers", None)
                if headers is not None:
                    for k, v in dict(headers).items():
                        if k.lower() == "content-type":
                            ct = v
                            break
                return status, bytes(body), ct

            validation_handler = _rust_validation_handler

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
            self.redirect_slashes,
            self.max_request_size,
            not_found_handler,
            self,
            validation_handler,
        )

    # ------------------------------------------------------------------
    # ASGI __call__ — enables ``uvicorn myapp:app`` compatibility
    # ------------------------------------------------------------------

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        """ASGI entry point.

        fastapi-rs uses its own Rust/Axum server, so a full ASGI adapter
        is not needed.  Instead we:

        1. **lifespan** scope: drive startup/shutdown handlers directly.
        2. **http** scope: auto-start the Rust server on a free port in a
           background thread, then proxy every request to it via httpx.
        3. **websocket** scope: proxy via websockets library.

        This lets ``uvicorn myapp:app`` (and Starlette's TestClient) work
        out of the box without any code changes.
        """
        if scope["type"] == "lifespan":
            await self._asgi_lifespan(scope, receive, send)
            return

        if scope["type"] == "http":
            await self._asgi_ensure_server()
            await self._asgi_proxy_http(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await self._asgi_ensure_server()
            await self._asgi_proxy_websocket(scope, receive, send)
            return

    # ── lifespan ──────────────────────────────────────────────────────

    async def _asgi_lifespan(self, scope: dict, receive: Callable, send: Callable) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    if self.lifespan:
                        self._run_lifespan_startup()
                    self._run_startup_handlers()
                    await send({"type": "lifespan.startup.complete"})
                except Exception as exc:
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
                    return
            elif message["type"] == "lifespan.shutdown":
                try:
                    if hasattr(self, "_lifespan_cm") and self._lifespan_cm:
                        self._run_lifespan_shutdown()
                    self._run_shutdown_handlers()
                except Exception:
                    pass
                await send({"type": "lifespan.shutdown.complete"})
                return

    # ── server bootstrap ──────────────────────────────────────────────

    async def _asgi_ensure_server(self) -> None:
        """Start the Rust server in a background thread if not already running."""
        if hasattr(self, "_asgi_server_port"):
            return

        import socket
        import threading
        import time

        # Find a free port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self._asgi_server_port = port

        # Start server in a daemon thread
        t = threading.Thread(
            target=self.run,
            kwargs={"host": "127.0.0.1", "port": port},
            daemon=True,
        )
        t.start()

        # Wait for server readiness (up to 10 seconds)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._asgi_wait_for_server, port)

    @staticmethod
    def _asgi_wait_for_server(port: int, timeout: float = 10.0) -> None:
        import socket
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    return
            except (ConnectionRefusedError, OSError):
                time.sleep(0.05)
        raise RuntimeError(
            f"fastapi-rs ASGI adapter: Rust server did not start on port {port} "
            f"within {timeout}s"
        )

    # ── HTTP proxy ────────────────────────────────────────────────────

    async def _asgi_proxy_http(self, scope: dict, receive: Callable, send: Callable) -> None:
        import httpx

        # Reconstruct the URL
        path = scope.get("path", "/")
        qs = scope.get("query_string", b"")
        url = f"http://127.0.0.1:{self._asgi_server_port}{path}"
        if qs:
            url += f"?{qs.decode('latin-1')}"

        # Reconstruct headers
        headers_list = scope.get("headers", [])
        headers = {}
        for name_bytes, value_bytes in headers_list:
            name = name_bytes.decode("latin-1") if isinstance(name_bytes, bytes) else name_bytes
            value = value_bytes.decode("latin-1") if isinstance(value_bytes, bytes) else value_bytes
            # Skip hop-by-hop headers
            if name.lower() in ("host", "transfer-encoding"):
                continue
            headers[name] = value

        # Read the request body
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break

        method = scope.get("method", "GET")

        async with httpx.AsyncClient() as client:
            resp = await client.request(
                method=method,
                url=url,
                headers=headers,
                content=body,
                follow_redirects=False,
            )

        # Send response start
        resp_headers = [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in resp.headers.multi_items()
            if k.lower() not in ("transfer-encoding",)
        ]
        await send({
            "type": "http.response.start",
            "status": resp.status_code,
            "headers": resp_headers,
        })

        # Send response body
        await send({
            "type": "http.response.body",
            "body": resp.content,
        })

    # ── WebSocket proxy ───────────────────────────────────────────────

    async def _asgi_proxy_websocket(self, scope: dict, receive: Callable, send: Callable) -> None:
        """Proxy an ASGI WebSocket connection to the Rust server.

        Falls back to a rejection if the ``websockets`` library is not
        installed.
        """
        try:
            from websockets.asyncio.client import connect as ws_connect
        except ImportError:
            # No websockets library — accept then close with error
            await send({"type": "websocket.close", "code": 1011})
            return

        path = scope.get("path", "/")
        qs = scope.get("query_string", b"")
        ws_url = f"ws://127.0.0.1:{self._asgi_server_port}{path}"
        if qs:
            ws_url += f"?{qs.decode('latin-1')}"

        # Wait for the client to connect
        message = await receive()
        if message["type"] != "websocket.connect":
            return

        try:
            async with ws_connect(ws_url) as ws:
                await send({"type": "websocket.accept"})

                async def _forward_client_to_server():
                    while True:
                        msg = await receive()
                        if msg["type"] == "websocket.disconnect":
                            await ws.close()
                            return
                        if "text" in msg:
                            await ws.send(msg["text"])
                        elif "bytes" in msg:
                            await ws.send(msg["bytes"])

                async def _forward_server_to_client():
                    async for data in ws:
                        if isinstance(data, str):
                            await send({"type": "websocket.send", "text": data})
                        else:
                            await send({"type": "websocket.send", "bytes": data})

                # Run both directions concurrently
                await asyncio.gather(
                    _forward_client_to_server(),
                    _forward_server_to_client(),
                    return_exceptions=True,
                )
        except Exception:
            try:
                await send({"type": "websocket.close", "code": 1011})
            except Exception:
                pass
