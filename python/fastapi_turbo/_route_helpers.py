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


def _maybe_print_debug_traceback(app, exc):
    """When app.debug is True, print the full traceback to stderr before
    the exception is routed to a handler. Matches FastAPI's ``debug=True``
    developer-ergonomics behavior.

    HTTPException is never traceback-printed — those are normal control flow.
    """
    if app is None or not getattr(app, "debug", False):
        return
    try:
        from fastapi_turbo.exceptions import HTTPException
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


def _wrap_response_class(result, response_class):
    """Wrap a bare handler result (dict/list/str/etc.) in a response_class.

    If the handler already returned a Response instance, leave it alone
    (Starlette semantics: user-returned Response always wins). Response
    classes differ in their constructor:
    ``RedirectResponse(url=...)`` / ``FileResponse(path=...)`` /
    everything else ``(content=...)``.
    """
    if response_class is None or result is None:
        return result
    # If result is already a Response-like object, don't double-wrap
    if hasattr(result, "status_code") and hasattr(result, "body"):
        return result
    _name = getattr(response_class, "__name__", "")
    if _name == "RedirectResponse":
        return response_class(url=result)
    if _name == "FileResponse":
        return response_class(path=result)
    return response_class(content=result)


def _apply_status_code(result, status_code: int):
    """Apply a route-declared `status_code=N` to the result.

    If the handler already returned a Response (or a Response-like), we
    set its status_code directly. Otherwise, wrap the bare return value
    in a JSONResponse with the declared status.
    """
    if result is None:
        # None + declared status_code → empty response with that status.
        from fastapi_turbo.responses import Response as _R
        return _R(content=b"", status_code=status_code)
    if hasattr(result, "status_code") and hasattr(result, "body"):
        # Only override if handler didn't explicitly set a non-default
        # code. RedirectResponse defaults to 307; ``status_code=302`` on
        # the route must win over that default.
        try:
            current = int(result.status_code)
            _default_codes = {200, 307}  # sensible defaults we can override
            if current in _default_codes:
                result.status_code = status_code
        except Exception:
            pass
        return result
    # Bare dict/list/str → wrap as JSONResponse with the declared status.
    from fastapi_turbo.responses import JSONResponse as _J
    return _J(content=result, status_code=status_code)


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


def _looks_like_body(annotation) -> bool:
    """Loose 'is this a Body param by default?' check for the custom-
    route-class path. Mirrors FA's default-body heuristic (scalars →
    query, anything structurally richer → body):

      * ``BaseModel`` subclass
      * ``@dataclass`` class
      * ``TypedDict`` subclass (detected via ``__total__`` + dict base)
      * bare ``dict`` / typed ``dict[...]`` / generic ``Mapping[...]``
      * ``list[T]`` / ``set[T]`` / ``frozenset[T]`` / ``tuple[T, ...]``
        where ``T`` is itself body-typed (e.g. ``list[Item]``)
      * ``Annotated[T, ...]`` — unwrap and recurse
      * ``T | None`` / ``Optional[T]`` / ``Union[T, …]`` — body if any
        non-None arm is body-typed
    """
    try:
        import typing as _tp
        from pydantic import BaseModel as _BM

        if annotation is None:
            return False

        # Unwrap ``Annotated[T, ...]`` — the metadata doesn't change
        # body-ness; the real type is the first arg.
        origin = _tp.get_origin(annotation)
        if origin is _tp.Annotated:
            inner = _tp.get_args(annotation)
            if inner:
                return _looks_like_body(inner[0])

        # Union / Optional: recurse into non-None arms.
        if origin is _tp.Union:
            non_none = [a for a in _tp.get_args(annotation) if a is not type(None)]
            return any(_looks_like_body(a) for a in non_none)

        if isinstance(annotation, type) and issubclass(annotation, _BM):
            return True

        # TypedDict: subclasses expose ``__required_keys__`` /
        # ``__total__`` and inherit from ``dict``. The stable marker
        # across 3.9 → 3.13 is ``__total__`` + being a subclass of
        # ``dict`` without being ``dict`` itself.
        if (
            isinstance(annotation, type)
            and annotation is not dict
            and issubclass(annotation, dict)
            and hasattr(annotation, "__total__")
        ):
            return True

        import dataclasses as _dc
        if isinstance(annotation, type) and _dc.is_dataclass(annotation):
            return True

        # Bare ``dict`` / ``list`` / ``set`` / ``frozenset`` / ``tuple``
        # without a parameter type — FA treats these as body (arbitrary
        # structures aren't parseable from a query string).
        if annotation in (dict, list, set, frozenset, tuple):
            return True

        import collections.abc as _cabc
        if origin in (dict, _cabc.Mapping, _cabc.MutableMapping):
            return True

        # list[T] / set[T] / frozenset[T] / tuple[T, ...] where T is
        # itself body-typed. Scalar containers like ``list[int]`` /
        # ``list[str]`` stay query (matches FA).
        if origin in (list, set, frozenset, tuple):
            args = _tp.get_args(annotation)
            if not args:
                # Bare ``list`` / ``set`` without item type — FA treats
                # these as body (unparseable from query string).
                return True
            return any(_looks_like_body(a) for a in args if a is not Ellipsis)

        return False
    except Exception:
        return False




def _build_real_default_route_handler(route, app):
    """``async (request) -> Response`` via REAL FastAPI's pipeline for this
    route — what ``super().get_route_handler()`` hands a custom route class.
    Builds a plain real ``fastapi.routing.APIRoute`` from the route's metadata
    (mirroring ``_delegated_route_info``) and wraps its handler with the three
    AsyncExitStack scopes real FA's handler reads off ``request.scope``."""
    from contextlib import AsyncExitStack as _AES

    from fastapi_turbo.applications import _real_fastapi

    methods = [
        m
        for m in (getattr(route, "methods", None) or ["GET"])
        if m in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "TRACE")
    ] or ["GET"]
    real = _real_fastapi.routing.APIRoute(
        getattr(route, "path", "/"),
        route.endpoint,
        methods=methods,
        dependencies=getattr(route, "dependencies", None) or None,
        response_model=getattr(route, "response_model", None),
        status_code=(
            route.status_code
            if getattr(route, "status_code", None) not in (None, 200)
            else None
        ),
        response_class=getattr(route, "response_class", None)
        or _real_fastapi.datastructures.Default(_real_fastapi.responses.JSONResponse),
        response_model_include=getattr(route, "response_model_include", None),
        response_model_exclude=getattr(route, "response_model_exclude", None),
        response_model_by_alias=getattr(route, "response_model_by_alias", True),
        response_model_exclude_unset=getattr(route, "response_model_exclude_unset", False),
        response_model_exclude_defaults=getattr(
            route, "response_model_exclude_defaults", False
        ),
        response_model_exclude_none=getattr(route, "response_model_exclude_none", False),
    )
    real.dependency_overrides_provider = app
    inner = real.get_route_handler()

    async def default_handler(request):
        async with _AES() as _file_stack, _AES() as _inner_stack, _AES() as _function_stack:
            request.scope["fastapi_middleware_astack"] = _file_stack
            request.scope["fastapi_inner_astack"] = _inner_stack
            request.scope["fastapi_function_astack"] = _function_stack
            response = await inner(request)
            # Run background tasks before yield-dep teardown (FA order), then
            # clear so the door doesn't double-run them.
            _bg = getattr(response, "background", None)
            if _bg is not None:
                _bgr = _bg()
                if inspect.isawaitable(_bgr):
                    await _bgr
                response.background = None
            return response

    return default_handler


def _build_custom_route_handler_endpoint(route, app):
    """Return the endpoint that fastapi-turbo registers with Rust when a
    route's APIRoute subclass overrides ``get_route_handler``. The
    endpoint takes a single ``Request`` kwarg (Rust injects it via
    ``inject_request``) and delegates to the user's wrapper.

    On first call, builds ``original_route_handler`` via
    ``_build_default_route_handler`` and caches it on the route so
    subsequent requests reuse it. The user's ``get_route_handler``
    returns their coroutine, which closes over ``original_route_handler``
    and wraps the request before calling it.
    """
    from fastapi_turbo.responses import JSONResponse as _JR, Response as _Resp
    from fastapi_turbo.exceptions import (
        RequestValidationError as _RVE,
        HTTPException as _HE,
    )

    # Expose the builder so ``APIRoute._default_route_handler`` can
    # resolve it. ``get_route_handler``'s ``super().get_route_handler()``
    # call routes through this — it returns REAL FastAPI's route handler (a
    # plain real APIRoute built from this route's metadata), so a user's
    # wrapper (GzipRoute/TimedRoute) wraps real FA's full pipeline: byte-exact
    # body parsing, validation, response_model serialization. The clone
    # mini-pipeline (_build_default_route_handler) is no longer on this path.
    def _build_default() -> Callable:
        return _build_real_default_route_handler(route, app)

    route._fastapi_turbo_build_default_handler = _build_default  # type: ignore[attr-defined]

    _app_ref = app

    async def custom_route_endpoint(request):
        try:
            custom_handler = route.get_route_handler()
            response = await custom_handler(request)
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
