"""Route-handler helpers extracted from ``applications.py``.

These are the functions that operate on a single route at compile or
request time — response-model application, response-class wrapping,
status-code stamping, upload-file cleanup, debug traceback printing,
default/custom route-handler construction.

Every symbol is used from ``applications.py``. The extraction is
mechanical (the functions don't reference module-level state in
``applications.py``); splitting them out shrinks the core dispatch
file and groups the per-route concerns in one place.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Callable

_log = logging.getLogger("fastapi_turbo.applications")


# ── Starlette-compat route classification ───────────────────────────
# Pre-constructed Starlette ``Route`` / ``WebSocketRoute`` / ``Mount``
# instances handed in via ``FastAPI(routes=[...])`` / ``APIRouter(
# routes=[...])`` / ``add_route`` are tagged here so the collection
# walker dispatches them with Starlette ASGI semantics.

def _looks_like_starlette_mount(route) -> bool:
    cls_name = type(route).__name__
    return cls_name == "Mount" or (
        hasattr(route, "routes")
        and hasattr(route, "app")
        and not hasattr(route, "endpoint")
    )


def _looks_like_starlette_websocket_route(route) -> bool:
    return (
        getattr(route, "_is_websocket", False)
        or type(route).__name__ == "WebSocketRoute"
    )


def _is_included_router(route) -> bool:
    """True for FastAPI 0.138's ``_IncludedRouter`` marker route.

    ``include_router`` appends one of these to ``self.router.routes`` (real
    FastAPI parity) so ``Router._match_low_priority`` can reach included
    frontend / low-priority routes. It carries no ``.path`` and is walked via
    ``_included_routers`` during the door flatten, so route-collection and the
    ``routes`` property skip it. Match by class name to avoid a hard import."""
    return type(route).__name__ == "_IncludedRouter"


def _append_included_router(parent_router, child_router, include_meta) -> None:
    """Append a real FastAPI ``_IncludedRouter`` to ``parent_router.routes``.

    turbo tracks includes in its own ``_included_routers`` list (and mirrors
    shadow clones for ``routes`` parity), but FastAPI 0.138's
    ``Router._match_low_priority`` reaches an included router's frontend /
    low-priority routes ONLY through an ``_IncludedRouter`` sitting in
    ``routes``. Adding one here lets the door's 404 fallback serve included
    frontends via 100% real FastAPI machinery (matching, longest-prefix,
    caching, nesting) and satisfies upstream's cache-reuse test. It's skipped
    by route-collection / the ``routes`` property (see ``_is_included_router``),
    so it doesn't affect normal dispatch or OpenAPI. Best-effort: on any
    failure the direct/served frontend paths still work via the
    ``_included_routers`` walk."""
    try:
        from fastapi.routing import _IncludedRouter, _RouterIncludeContext
    except Exception:  # noqa: BLE001
        return
    meta = include_meta or {}
    kwargs: dict = {"prefix": meta.get("prefix", "") or ""}
    for key in (
        "tags",
        "dependencies",
        "responses",
        "callbacks",
        "deprecated",
        "include_in_schema",
        "default_response_class",
        "generate_unique_id_function",
    ):
        value = meta.get(key)
        if value is not None:
            kwargs[key] = value
    try:
        _normalize_gen_ids(child_router)
        ctx = _RouterIncludeContext.for_include(
            parent_router=parent_router,
            included_router=child_router,
            **kwargs,
        )
        # turbo's routers default ``generate_unique_id_function`` /
        # ``default_response_class`` to ``None`` (real FastAPI uses
        # ``Default(...)``). ``for_include`` folds the parent's ``None`` in via
        # ``get_value_or_default``, so the context inherits ``None`` — which
        # then crashes ``from_api_route`` (``None(route)``) for an included
        # low-priority ``APIRoute``. Restore the real defaults on the context
        # (it only affects the never-in-OpenAPI low-priority surface).
        _restore_context_defaults(ctx)
        parent_router.routes.append(
            _IncludedRouter(original_router=child_router, include_context=ctx)
        )
        if hasattr(parent_router, "_mark_routes_changed"):
            parent_router._mark_routes_changed()
    except Exception as exc:  # noqa: BLE001
        _log.debug("could not append _IncludedRouter: %r", exc)


def _restore_context_defaults(ctx) -> None:
    try:
        from fastapi.datastructures import Default, DefaultPlaceholder
        from fastapi.responses import JSONResponse
        from fastapi.utils import generate_unique_id
    except Exception:  # noqa: BLE001
        return
    gid = getattr(ctx, "generate_unique_id_function", None)
    if not (callable(gid) or isinstance(gid, DefaultPlaceholder)):
        ctx.generate_unique_id_function = Default(generate_unique_id)
    if getattr(ctx, "default_response_class", None) is None:
        ctx.default_response_class = Default(JSONResponse)


def _normalize_gen_ids(router) -> None:
    """FastAPI's ``_EffectiveRouteContext`` builders (``from_api_route`` /
    ``_build_effective_context``, reached whenever an ``_IncludedRouter``'s
    routes are surfaced for matching) compute ``route.unique_id`` via
    ``get_value_or_default(route.gen_id, included_router.gen_id,
    include_context.gen_id)`` and then *call* it. turbo's ``APIRoute`` and
    ``APIRouter`` store ``generate_unique_id_function`` as ``None`` (real
    FastAPI uses ``Default(...)``) to keep the door's OpenAPI operationIds
    stable, and ``get_value_or_default`` treats ``None`` as an explicit value —
    so the first ``None`` wins and ``None(route)`` raises.

    Restore the real ``Default(generate_unique_id)`` on every router and
    ``APIRoute`` in the tree that has ``None`` there. Nothing in turbo reads
    ``generate_unique_id_function`` (operationIds come from the eagerly-computed
    ``operation_id`` / ``unique_id``), so this is invisible to the door's schema
    — it only lets FastAPI's effective-route machinery run for the 404-fallback
    matcher. Also restores ``default_response_class`` on routers that carry
    low-priority routes (the ``_FrontendRouteGroup`` case, never in OpenAPI).
    Walks nested includes."""
    try:
        from fastapi.routing import APIRoute
        from fastapi.datastructures import Default, DefaultPlaceholder
        from fastapi.responses import JSONResponse
        from fastapi.utils import generate_unique_id
    except Exception:  # noqa: BLE001
        return

    def _fix_gen_id(obj) -> None:
        v = getattr(obj, "generate_unique_id_function", None)
        if not (callable(v) or isinstance(v, DefaultPlaceholder)):
            obj.generate_unique_id_function = Default(generate_unique_id)

    def _walk(rt) -> None:
        _fix_gen_id(rt)
        if (
            getattr(rt, "_low_priority_routes", None)
            and getattr(rt, "default_response_class", None) is None
        ):
            rt.default_response_class = Default(JSONResponse)
        for r in list(getattr(rt, "routes", None) or ()):
            if isinstance(r, APIRoute):
                _fix_gen_id(r)
        for lp in getattr(rt, "_low_priority_routes", None) or ():
            if isinstance(lp, APIRoute):
                _fix_gen_id(lp)
        for entry in getattr(rt, "_included_routers", None) or ():
            _walk(entry[0])

    _walk(router)


def normalize_app_gen_ids(app) -> None:
    """Normalize ``generate_unique_id_function`` across the whole app's router
    tree so the 404-fallback matcher can drive routes added after the socket
    server started (turbo's TestClient binds a fixed ``app.run()`` server).
    Idempotent and cheap — already-normalized routes are skipped."""
    _normalize_gen_ids(app.router)
    for entry in getattr(app, "_included_routers", None) or ():
        _normalize_gen_ids(entry[0])


def _mark_starlette_compat_route(route) -> None:
    from fastapi_turbo._ws_support import _adapt_websocket_endpoint_class

    if _looks_like_starlette_mount(route):
        try:
            route._fastapi_turbo_starlette_mount = True  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            pass
        return
    if _looks_like_starlette_websocket_route(route):
        try:
            route.endpoint = _adapt_websocket_endpoint_class(route.endpoint)
            route._is_websocket = True  # type: ignore[attr-defined]
            route._fastapi_turbo_starlette_websocket = True  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            pass
        return
    try:
        route._fastapi_turbo_starlette_passthrough = True  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        pass


def _is_async_callable(func) -> bool:
    """Return True if ``func`` is async, including when wrapped via ``@wraps``.

    ``@noop_wrap`` / similar sync decorators that call an inner ``async def``
    produce a SYNC wrapper whose ``__wrapped__`` points at the real async
    function. ``inspect.iscoroutinefunction`` returns False on the outer
    wrapper but calling it still returns a coroutine, so treat those as
    async. Also handles class-instance callables.
    """
    if func is None:
        return False
    if inspect.iscoroutinefunction(func):
        return True
    # Walk __wrapped__ chain
    probe = func
    for _ in range(10):
        nxt = getattr(probe, "__wrapped__", None)
        if nxt is None or nxt is probe:
            break
        probe = nxt
        if inspect.iscoroutinefunction(probe):
            return True
    # Class instance: check __call__ and its wrapped chain.
    call = getattr(func, "__call__", None)
    if call is not None and not isinstance(func, type):
        if inspect.iscoroutinefunction(call):
            return True
        probe = call
        for _ in range(10):
            nxt = getattr(probe, "__wrapped__", None)
            if nxt is None or nxt is probe:
                break
            probe = nxt
            if inspect.iscoroutinefunction(probe):
                return True
    return False


def _apply_response_model(
    result,
    response_model,
    include=None,
    exclude=None,
    exclude_unset=False,
    exclude_defaults=False,
    exclude_none=False,
    by_alias=True,
    endpoint_ctx=None,
):
    """Filter a handler result through a response_model Pydantic class.

    by_alias=True (FastAPI default) — honor Pydantic Field(alias=...) and
    Field(serialization_alias=...) in output. Critical for APIs that use
    aliased fields (camelCase over snake_case, etc.).
    """
    if response_model is None:
        return result
    # Still validate ``None`` against the response_model — FA raises
    # ``ResponseValidationError`` when a non-Optional model gets None,
    # and for ``Optional[Model]`` Pydantic passes it through.

    # Skip when the handler returned a Response object directly —
    # Starlette Response / StreamingResponse / FileResponse etc. are
    # pass-through, FA doesn't try to re-serialize them.
    try:
        from fastapi_turbo.responses import Response as _RespBase
        if isinstance(result, _RespBase):
            return result
    except ImportError:
        pass

    # Skip for generators / async generators — these flow into a
    # ``StreamingResponse`` wrapper (via ``response_class``) rather
    # than through response_model validation. Running TypeAdapter
    # serialization on an ``Iterable[bytes]`` tries to utf-8-decode
    # binary chunks and explodes.
    import inspect as _inspect_mod
    if _inspect_mod.isgenerator(result) or _inspect_mod.isasyncgen(result):
        return result

    has_filters = (
        include is not None
        or exclude is not None
        or exclude_unset
        or exclude_defaults
        or exclude_none
        or by_alias is False
    )

    # Generic aliases like `list[UserOut]`, `dict[str, UserOut]`,
    # `Optional[UserOut]` don't have `.model_validate`. Route them through
    # Pydantic's TypeAdapter so that lists of SQLAlchemy ORM instances are
    # dumped via `from_attributes`.
    from fastapi_turbo.exceptions import (
        ResponseValidationError as _RVE,
        _DoorResponseValidationError as _DResVE,
    )
    if not hasattr(response_model, "model_validate"):
        from pydantic import TypeAdapter, ValidationError as _PyVE
        try:
            ta = TypeAdapter(response_model)
        except Exception as _exc:  # noqa: BLE001
            _log.debug("silent catch in applications: %r", _exc)
            return result
        dump_kwargs = {"by_alias": by_alias, "mode": "json"}
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
        try:
            validated = ta.validate_python(result, from_attributes=True)
        except _PyVE as exc:
            # FA parity: response-validation errors prepend ``"response"``
            # to ``loc`` so tests asserting ``loc == ("response", "name")``
            # (``test_validator_is_cloned``) pass. Strip pydantic's ``url``
            # field since FA doesn't emit it.
            _prefixed = [
                {
                    k: v for k, v in {
                        **_e,
                        "loc": ("response", *tuple(_e.get("loc", ()))),
                    }.items()
                    if k != "url"
                }
                for _e in exc.errors()
            ]
            raise _DResVE(errors=_prefixed, body=result, endpoint_ctx=endpoint_ctx) from None
        return ta.dump_python(validated, **dump_kwargs)

    try:
        # Always go through model_validate + model_dump when by_alias matters —
        # we can't take the fast "strip extra keys" path because field names
        # differ from aliases.
        fast_path_ok = not has_filters and not _model_has_aliases(response_model)

        import dataclasses as _dc
        if isinstance(result, dict):
            # FA always validates the handler's return against
            # response_model, even in the fast path — a handler that
            # returns ``{"price": "foo"}`` for a ``price: float`` field
            # must raise ``ResponseValidationError``. We validate then
            # re-emit the validated dict (preserving defaults).
            validated = response_model.model_validate(result)
        elif hasattr(result, "model_dump"):
            if fast_path_ok and type(result) is response_model:
                return result.model_dump(mode="json")
            # If `result` is ALREADY an instance of response_model, use it
            # directly — round-tripping via model_dump()+model_validate()
            # would mark ALL fields as explicitly set, defeating
            # exclude_unset / exclude_defaults.
            if type(result) is response_model:
                validated = result
            elif (
                isinstance(response_model, type)
                and isinstance(result, response_model)
                and (exclude_unset or exclude_defaults or exclude_none)
            ):
                # FA parity: ``ModelSubclass`` returned by handler with
                # ``response_model=Model`` and ``exclude_unset=True``
                # should preserve which fields the HANDLER explicitly
                # set. Dumping subclass + filtering to response_model's
                # field set preserves ``model_fields_set``.
                _sub_dump = result.model_dump(
                    by_alias=by_alias, mode="json",
                    exclude_unset=exclude_unset,
                    exclude_defaults=exclude_defaults,
                    exclude_none=exclude_none,
                    include=include, exclude=exclude,
                )
                _rm_fields = set(getattr(response_model, "model_fields", {}).keys())
                return {k: v for k, v in _sub_dump.items() if k in _rm_fields}
            else:
                # FA parity: when ``response_model`` declares
                # ``model_config = {"from_attributes": True}``, pull the
                # response fields via attribute access on the handler's
                # return value — needed for ``@property``-derived fields
                # (e.g. ``Person.full_name`` computed from ``name`` + ``lastname``).
                _from_attrs = False
                _cfg = getattr(response_model, "model_config", None)
                if isinstance(_cfg, dict) and _cfg.get("from_attributes"):
                    _from_attrs = True
                if _from_attrs:
                    validated = response_model.model_validate(
                        result, from_attributes=True
                    )
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
        elif hasattr(response_model, "model_validate"):
            # Arbitrary object (typical SQLAlchemy ORM instance) — let
            # Pydantic `from_attributes=True` pick up columns via attribute
            # access. FastAPI's default behavior when `response_model` is a
            # Pydantic model and the returned value is anything else.
            validated = response_model.model_validate(result)
        else:
            return result

        dump_kwargs = {"by_alias": by_alias, "mode": "json"}
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
    except _RVE:
        raise
    except Exception as exc:  # noqa: BLE001
        # FA raises ``ResponseValidationError`` when the handler's return
        # can't satisfy ``response_model``. Promote pydantic
        # ``ValidationError`` to our RVE; swallow anything else (keeps
        # legacy behaviour for edge cases).
        try:
            from pydantic import ValidationError as _PyVE
            if isinstance(exc, _PyVE):
                # FA parity: prepend ``"response"`` to ``loc`` and
                # strip pydantic's ``url`` field.
                _prefixed = [
                    {
                        k: v for k, v in {
                            **_e,
                            "loc": ("response", *tuple(_e.get("loc", ()))),
                        }.items()
                        if k != "url"
                    }
                    for _e in exc.errors()
                ]
                raise _DResVE(errors=_prefixed, body=result, endpoint_ctx=endpoint_ctx) from None
        except ImportError:
            pass
    return result


def _model_needs_full_dump(model_cls) -> bool:
    """True if the model has aliases, computed fields, or custom serializers.

    When any of these are present, the 'strip extra keys' fast path is unsafe —
    we must go through model_validate + model_dump so Pydantic can:
      - rename fields via alias / serialization_alias
      - include computed_field properties in output
      - run field_serializer / model_serializer hooks

    Cached per class on the class itself — paid once per model, not per request.
    """
    cached = getattr(model_cls, "__fastapi_turbo_needs_full_dump__", None)
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
        model_cls.__fastapi_turbo_needs_full_dump__ = needs
    except Exception:
        pass
    return needs


# Back-compat alias for existing callers
_model_has_aliases = _model_needs_full_dump


def _close_upload_files(kwargs: dict) -> None:
    """Call ``.close()`` on any UploadFile-like objects passed as kwargs.
    Matches Starlette's behaviour of closing uploads after the
    response is built so tests can assert ``file.closed``.
    """
    for _v in list(kwargs.values()):
        _close_one_upload(_v)


def _close_one_upload(obj) -> None:
    if obj is None:
        return
    if isinstance(obj, list):
        for item in obj:
            _close_one_upload(item)
        return
    if hasattr(obj, "close") and hasattr(obj, "filename"):
        try:
            _r = obj.close()
            if hasattr(_r, "__await__"):
                try:
                    _r.send(None)
                except (StopIteration, Exception):
                    pass
        except Exception as _exc:  # noqa: BLE001
            _log.debug("silent catch in applications: %r", _exc)


def _has_overridden_get_route_handler(route) -> bool:
    """True when ``type(route).get_route_handler`` is NOT the default
    inherited from APIRoute. This is the marker FA uses to let users
    wrap the request pipeline — GzipRoute decompresses bodies,
    TimedRoute injects timing headers, etc. When detected, the app
    builds a Python-side adapter (``_build_custom_route_handler_endpoint``)
    that hands the request to the user's wrapper instead of the
    direct Rust dispatch.
    """
    try:
        from fastapi_turbo.routing import APIRoute  # noqa: PLC0415 — local import to avoid circular
        return type(route).get_route_handler is not APIRoute.get_route_handler
    except AttributeError:
        return False
    except ImportError:
        return False


def _build_custom_route_handler_endpoint(route, app):
    """Return the endpoint that fastapi-turbo registers with Rust when a
    route's APIRoute subclass overrides ``get_route_handler``. The
    endpoint takes a single ``Request`` kwarg (Rust injects it via
    ``inject_request``) and delegates to the user's wrapper.

    The turbo ``APIRoute`` IS a real ``fastapi.routing.APIRoute``
    subclass, so the user's ``super().get_route_handler()`` returns real
    FastAPI's full pipeline handler: byte-exact body parsing, validation,
    dependency resolution, response_model serialization. Real's handler
    requires the AsyncExitStack scope keys (it raises if
    ``fastapi_middleware_astack`` is missing — normally installed by
    AsyncExitStackMiddleware, which the door bypasses), so this adapter
    sets them up per request and runs background tasks before yield-dep
    teardown (then clears them so the door doesn't double-run).
    """
    from contextlib import AsyncExitStack as _AES

    from fastapi_turbo.responses import JSONResponse as _JR, Response as _Resp
    from fastapi_turbo.exceptions import (
        RequestValidationError as _RVE,
        HTTPException as _HE,
    )

    # Real's get_route_handler() resolves ``app.dependency_overrides``
    # through this provider (a real FastAPI subclass) — point it at the
    # app so overrides registered at any time are honored.
    try:
        route.dependency_overrides_provider = app
    except (AttributeError, TypeError):
        pass

    _app_ref = app

    async def custom_route_endpoint(request):
        try:
            # Rebuilt per request: the user's ``get_route_handler`` is
            # the documented FA extension hook, and rebuilding lets a
            # request observe override/provider changes (TestClient).
            custom_handler = route.get_route_handler()
            async with (
                _AES() as _file_stack,
                _AES() as _inner_stack,
                _AES() as _function_stack,
            ):
                request.scope["fastapi_middleware_astack"] = _file_stack
                request.scope["fastapi_inner_astack"] = _inner_stack
                request.scope["fastapi_function_astack"] = _function_stack
                response = await custom_handler(request)
                # Run background tasks before yield-dep teardown (FA
                # order), then clear so the door doesn't double-run them.
                _bg = getattr(response, "background", None)
                if _bg is not None:
                    _bgr = _bg()
                    if inspect.isawaitable(_bgr):
                        await _bgr
                    response.background = None
        except _HE as exc:
            # Route through the app's registered HTTPException handler
            # (TestClient path) so custom ``detail`` dicts surface.
            if _app_ref is not None and _app_ref.exception_handlers:
                hdl = _app_ref._invoke_exception_handler(exc)
                if hdl is not None:
                    return hdl
            return _JR(content={"detail": exc.detail}, status_code=exc.status_code)
        except _RVE as exc:
            if _app_ref is not None and _RVE in _app_ref.exception_handlers:
                hdl = _app_ref._invoke_exception_handler_strict(exc)
                if hdl is not None:
                    return hdl
            return _JR(content={"detail": exc.errors()}, status_code=422)
        if not isinstance(response, _Resp):
            return _JR(content=response)
        return response

    custom_route_endpoint._fastapi_turbo_custom_route_class = True  # type: ignore[attr-defined]
    return custom_route_endpoint
