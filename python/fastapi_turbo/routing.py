"""Routing primitives matching FastAPI's interface."""

from __future__ import annotations

import inspect
import re
import types
import typing
from typing import Any, Callable, Sequence
from urllib.parse import quote

# REAL pip FastAPI. This module is imported during ``fastapi_turbo``
# package init (via ``applications.py``, which captures ``import
# fastapi`` at its line 18) — always BEFORE ``compat.install()`` patches
# the accelerated entry points onto the real package — so class
# statements below bind the GENUINE base classes. ``APIRoute`` below is
# a THIN SUBCLASS of the real one.
import fastapi as _real_fastapi


def _is_union_origin(origin: Any) -> bool:
    return origin is typing.Union or origin is types.UnionType


def _default_generate_unique_id(route: "APIRoute", method: str) -> str:
    """Default function to generate a unique operation ID for OpenAPI."""
    return f"{route.name}_{method.lower()}"


# WebSocket-route support (incl. ``_safe_signature``, used by the
# decoration-time assertion helpers below) lives in the door-scope
# ``_ws_support`` module — WS routes never build a real FastAPI route.
from fastapi_turbo._ws_support import (  # noqa: F401 — re-exported for compat
    WSRoute,
    _adapt_websocket_endpoint_class,
    _is_websocket_endpoint_class,
    _maybe_await_ws_result,
    _safe_signature,
    _ws_check_scope_mismatch,
)


# Starlette-compat route classification lives with the other per-route
# helpers in ``_route_helpers`` (used by ``routes=`` kwargs + add_route
# here, and by the collection walker in ``applications``).
from fastapi_turbo._route_helpers import (  # noqa: F401 — re-exported for compat
    _looks_like_starlette_mount,
    _looks_like_starlette_websocket_route,
    _mark_starlette_compat_route,
)


_UNSET = object()
"""Sentinel for distinguishing ``response_model=None`` (explicit — skip
model validation) from ``response_model`` being omitted (auto-derive from
the return annotation). FA does the same — ``default=Default(None)``."""


def _unset_to_none(value: Any) -> Any:
    """Map real FastAPI's "kwarg not explicitly set" marker to ``None``.

    Real ``APIRoute.__init__`` stores a ``DefaultPlaceholder`` for unset
    ``response_class`` / ``strict_content_type``; the door's collection
    layer keys its cascades (route → router → include → app) off plain
    ``None``."""
    if isinstance(value, _real_fastapi.datastructures.DefaultPlaceholder):
        return None
    return value


def _derive_return_annotation(endpoint: Callable) -> Any:
    """Clone-style return-annotation lookup (``get_type_hints`` so
    stringified annotations resolve)."""
    try:
        hints = typing.get_type_hints(endpoint, include_extras=False)
        ra = hints.get("return")
        if ra is None:
            ra = inspect.signature(endpoint).return_annotation
            if ra is inspect.Signature.empty:
                ra = None
        return ra
    except (TypeError, ValueError, NameError):
        return None


def _is_streaming_annotation(ann: Any) -> bool:
    import collections.abc as _abc
    return typing.get_origin(ann) in (
        _abc.AsyncIterable, _abc.AsyncIterator, _abc.AsyncGenerator,
        _abc.Iterable, _abc.Iterator, _abc.Generator,
    )


# Kwargs forwarded verbatim to real ``APIRoute.__init__``. Everything
# else that lands in ``**kwargs`` is swallowed (the clone tolerated
# unknown kwargs; real raises TypeError).
_REAL_APIROUTE_PASSTHROUGH = frozenset({
    "status_code", "tags", "dependencies", "summary", "description",
    "response_description", "responses", "deprecated", "include_in_schema",
    "response_model_include", "response_model_exclude",
    "response_model_by_alias", "response_model_exclude_unset",
    "response_model_exclude_defaults", "response_model_exclude_none",
    "dependency_overrides_provider",
})


class APIRoute(_real_fastapi.routing.APIRoute):
    """Thin subclass of REAL FastAPI's ``APIRoute``.

    Real ``__init__`` does the heavy lifting — signature introspection
    (``dependant``), ``response_field``, path compilation, the whole
    decoration-time validation battery, and ``self.app`` (real FastAPI's
    request pipeline, so user ``route_class`` subclasses overriding
    ``get_route_handler`` wrap real FastAPI's handler natively via
    ``super().get_route_handler()``). This subclass only papers over the
    door's read-contract differences:

    - turbo-extra kwargs (``security`` / ``servers`` / ``external_docs``)
      are stored as plain attrs; unknown kwargs are swallowed like the
      clone did;
    - ``methods`` is re-stamped as an ordered UPPER **list** (real stores
      a set; the collection layer indexes ``methods[0]`` and the Rust
      door extracts a ``Vec<String>``);
    - ``generate_unique_id_function`` is stored RAW (``None`` when unset
      — real's ``DefaultPlaceholder`` would otherwise be unwrapped by the
      app-level cascade and rewrite every operationId) and
      ``operation_id`` is computed eagerly, incl. the legacy
      ``(route, method)`` two-arg fallback;
    - streaming return annotations (``AsyncIterable[T]`` etc.) stay on
      ``response_model`` — real 0.136 moves them to ``stream_item_type``,
      but the door's SSE/NDJSON OpenAPI emitters (``_oa_stream_info``)
      and ``_build_stream_handler`` read the raw annotation;
    - ``openapi_extra`` / ``callbacks`` default to ``{}`` / ``[]``.
    """

    def __init__(
        self,
        path: str,
        endpoint: Callable,
        *,
        methods: list[str] | None = None,
        response_model: Any = _UNSET,
        name: str | None = None,
        operation_id: str | None = None,
        generate_unique_id_function: Callable | None = None,
        response_class: Any = None,
        openapi_extra: dict | None = None,
        callbacks: list | None = None,
        strict_content_type: bool | None = None,
        security: list | None = None,
        servers: list[dict[str, Any]] | None = None,
        external_docs: dict[str, Any] | None = None,
        **kwargs: Any,
    ):
        super_kwargs = {
            k: kwargs.pop(k) for k in list(kwargs) if k in _REAL_APIROUTE_PASSTHROUGH
        }
        if kwargs:
            import logging
            logging.getLogger("fastapi_turbo.routing").debug(
                "APIRoute(%r): ignoring unknown kwargs %r", path, sorted(kwargs)
            )
        if response_model is not _UNSET:
            # Explicit value (incl. explicit ``None`` = "skip response-
            # model filtering"). When omitted, real's ``Default(None)``
            # derivation reproduces the clone's: return annotation,
            # ``-> Response`` drop, docstring description, decoration-time
            # "Invalid args for response field" error.
            super_kwargs["response_model"] = response_model
        if response_class is not None:
            super_kwargs["response_class"] = response_class
        if strict_content_type is not None:
            super_kwargs["strict_content_type"] = strict_content_type
        if not name:
            # Clone-style naming: dig ``functools.partial``'s wrapped
            # function (real ``get_name`` would say "partial"), then the
            # class name for callable instances.
            ep = endpoint
            inner = getattr(ep, "func", None)
            if inner is not None:
                ep = inner
            name = getattr(ep, "__name__", None) or type(endpoint).__name__
        # ``generate_unique_id_function`` is deliberately NOT forwarded:
        # real's __init__ would call it with one arg to compute
        # ``unique_id`` and a legacy ``(route, method)`` fn would raise.
        super().__init__(
            path,
            endpoint,
            methods=list(methods or ["GET"]),
            name=name,
            operation_id=operation_id,
            callbacks=callbacks,
            openapi_extra=openapi_extra,
            **super_kwargs,
        )

        # ── Door read-contract re-stamps ─────────────────────────────
        self.methods = [m.upper() for m in (methods or ["GET"])]
        if operation_id is None and generate_unique_id_function is not None:
            try:
                self.operation_id = generate_unique_id_function(self)
            except TypeError:
                # Fall back to legacy ``(route, method)`` callers.
                self.operation_id = generate_unique_id_function(
                    self, self.methods[0] if self.methods else "get"
                )
        self.generate_unique_id_function = generate_unique_id_function
        self.openapi_extra = openapi_extra or {}
        self.callbacks = callbacks or []
        self.security = security  # None = auto-derive; [] = disable; non-empty = override
        self.servers = servers  # None = inherit from app
        self.external_docs = external_docs
        if response_model is _UNSET:
            if self.response_model is None:
                derived = _derive_return_annotation(endpoint)
                if derived is not None and _is_streaming_annotation(derived):
                    self.response_model = derived
            elif _derive_return_annotation(endpoint) is None:
                # Clone-parity leniency: real's derivation keeps
                # UNRESOLVABLE forward refs (PEP 563 string annotations
                # referencing function-local classes) as lenient
                # ``ForwardRef``s whose mock validator explodes at request
                # time — upstream FastAPI 500s on this pattern. The clone
                # dropped such annotations (``get_type_hints`` raised
                # ``NameError`` → no response_model), so keep that.
                self.response_model = None
                self.response_field = None


class APIRouter(_real_fastapi.routing.APIRouter):
    """Route collection that mirrors FastAPI's APIRouter.

    A thin subclass of REAL FastAPI's ``APIRouter`` (so ``isinstance``
    checks against real classes pass and the shim binding upgrades for
    free). Registration stays turbo-DEFERRED: real's eager
    include-flatten is NOT used — the door's collection walker reads
    ``routes`` + ``_included_routers`` and flattens at startup, so every
    registration method below overrides real's.

    ``super().__init__()`` runs with DEFAULTS only: real asserts on
    prefixes the clone tolerated (e.g. a trailing ``/``, which the
    walker normalizes at join time) and wraps unset kwargs in
    ``DefaultPlaceholder``s the cascade consumers don't expect. Every
    turbo-visible field is re-stamped with clone semantics (raw ``None``
    = "unset") right after.
    """

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
        strict_content_type: bool | None = None,
        routes: Sequence | None = None,
        **kwargs: Any,
    ):
        super().__init__()
        self.routes: list = []
        self._included_routers: list[tuple[APIRouter, str, list[str], dict]] = []
        self.strict_content_type = strict_content_type
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
        # FA / Starlette parity: ``APIRouter(routes=[...])`` accepts
        # pre-constructed Starlette ``Route`` / ``WebSocketRoute`` /
        # ``Mount`` instances and registers them on this router so
        # they're served like any decorator-registered APIRoute.
        # Earlier the kwarg was accepted via ``**kwargs`` and silently
        # dropped (R52 finding 1). Mark each as Starlette-passthrough
        # so route collection uses ``await endpoint(request)`` rather than
        # FastAPI's parameter-injection introspection.
        if routes:
            for _r in routes:
                _mark_starlette_compat_route(_r)
                self.routes.append(_r)

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
        # Honour ``APIRouter(route_class=...)`` — users subclass APIRoute
        # to attach custom attrs / override request handling.
        route_cls = self.route_class or APIRoute
        route = route_cls(path, endpoint, methods=methods, **kwargs)
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

    def route(
        self,
        path: str,
        methods: list[str] | None = None,
        name: str | None = None,
        include_in_schema: bool = True,
        **kwargs: Any,
    ):
        """Generic route decorator (Starlette-compatible).

        Usage::

            @router.route("/health", methods=["GET", "POST"])
            async def health(request): ...
        """

        def decorator(func: Callable) -> Callable:
            self.add_route(
                path,
                func,
                methods=methods,
                name=name,
                include_in_schema=include_in_schema,
                **kwargs,
            )
            return func

        return decorator

    def add_route(
        self,
        path: str,
        endpoint: Callable,
        methods: list[str] | None = None,
        name: str | None = None,
        include_in_schema: bool = True,
        **kwargs: Any,
    ) -> None:
        """Imperative generic route registration (Starlette-compatible)."""
        kwargs.setdefault("response_model", None)
        route = APIRoute(
            path,
            endpoint,
            methods=methods or ["GET"],
            name=name,
            include_in_schema=include_in_schema,
            **kwargs,
        )
        _mark_starlette_compat_route(route)
        self.routes.append(route)

    # ------------------------------------------------------------------
    # WebSocket routes
    # ------------------------------------------------------------------

    def add_websocket_route(
        self,
        path: str,
        endpoint: Callable,
        name: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Register a WebSocket route."""
        endpoint = _adapt_websocket_endpoint_class(endpoint)
        # FastAPI 0.120+ scope rule check. Raise FastAPIError at
        # decoration time when a request-scope yield-dep depends on a
        # function-scope yield-dep — matches FA parity so tests asserting
        # ``pytest.raises(FastAPIError)`` around the decorator fire.
        _ws_check_scope_mismatch(endpoint)
        # WS endpoints are typed against turbo's standalone WebSocket
        # class, which a real ``fastapi.routing.APIRoute`` rejects at
        # construction — register a lightweight ``WSRoute`` holder
        # (already tagged ``_is_websocket=True``) instead.
        route = WSRoute(path, endpoint, name=name, **kwargs)
        self.routes.append(route)

    def add_api_websocket_route(
        self,
        path: str,
        endpoint: Callable,
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
        # FA parity: detect the ``router.include_router(router)`` typo
        # and raise at decoration time. The alternative (infinite
        # recursion at route-flatten time) produces an unhelpful error.
        assert router is not self, (
            "Cannot include the same APIRouter instance into itself. "
            "Did you mean to include a different router?"
        )
        # If the included router has ``deprecated=True`` on itself, that
        # should surface on every route reachable through this include.
        # The explicit ``deprecated=`` kwarg on include_router takes
        # priority when given.
        _effective_deprecated = (
            deprecated
            if deprecated is not None
            else getattr(router, "deprecated", None)
        )
        include_meta = {
            "prefix": prefix,
            "tags": tags or [],
            "dependencies": list(dependencies or []),
            "responses": responses or {},
            "deprecated": _effective_deprecated,
            "include_in_schema": include_in_schema,
            "default_response_class": default_response_class,
            "generate_unique_id_function": generate_unique_id_function,
            "callbacks": list(callbacks or []),
        }
        self._included_routers.append((router, prefix, tags or [], include_meta))

        # Eagerly mirror the included router's routes into ``self.routes``
        # as shadow clones with prefix-adjusted paths. Starlette/FA parity:
        # ``app.router.routes`` lists EVERY registered route (including
        # those reached via ``include_router``), so tests doing
        # ``for r in app.router.routes: ...`` see sub-routes at their
        # final paths. Shadow routes are tagged with
        # ``_is_included_shadow=True`` so ``_collect_routes_from_router``
        # skips them during the Rust flatten (avoids double-dispatch).
        import copy as _copy
        own_prefix = getattr(router, "prefix", "") or ""
        full_prefix = (prefix or "") + own_prefix

        def _stack_path(pfx: str, child: str) -> str:
            if not pfx:
                return child
            if not child:
                return pfx
            joined = pfx.rstrip("/") + "/" + child.lstrip("/")
            return joined or "/"

        # The shadow mirror exists ONLY for ``router.routes`` parity
        # (callers iterating routes see sub-routes at their final
        # paths); the door's flatten walks ``_included_routers`` with
        # the include_meta above for the real cascade. The old
        # response-class / deps stamps on the clones were write-only —
        # nothing read them — so the mirror is path-stacking only.
        def _mirror(src_router, pfx: str) -> None:
            for r in getattr(src_router, "routes", []):
                if getattr(r, "_is_included_shadow", False):
                    continue
                clone = _copy.copy(r)
                clone.path = _stack_path(pfx, getattr(r, "path", ""))
                clone._is_included_shadow = True
                self.routes.append(clone)
            for entry in getattr(src_router, "_included_routers", []):
                child_router, child_prefix = entry[0], entry[1]
                nested_prefix = _stack_path(
                    _stack_path(pfx, child_prefix or ""),
                    getattr(child_router, "prefix", "") or "",
                )
                _mirror(child_router, nested_prefix)

        _mirror(router, full_prefix)
