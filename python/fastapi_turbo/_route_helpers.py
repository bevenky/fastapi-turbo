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
    from fastapi_turbo.exceptions import ResponseValidationError as _RVE
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
            raise _RVE(errors=_prefixed, body=result, endpoint_ctx=endpoint_ctx) from None
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
                raise _RVE(errors=_prefixed, body=result, endpoint_ctx=endpoint_ctx) from None
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


def _build_default_route_handler(route, app):
    """Return an ``async (request) -> Response`` callable that runs
    the route's full pipeline (body parsing, param validation, endpoint
    call, response serialization) given a Starlette-style ``Request``.

    This is what ``APIRoute.get_route_handler()`` returns by default.
    Custom route-class subclasses wrap this in their own coroutine so
    the request is pre-processed before the pipeline runs — GzipRoute
    swaps it for a ``GzipRequest`` that decompresses on ``body()``,
    TimedRoute wraps the response with a timing header, etc.

    Scope: supports the subset FA's ``get_request_handler`` hits on
    the tutorial patterns — single ``Body()`` param (list/dict/primitive/
    Pydantic model), ``Request`` injection, ``HTTPException`` / Pydantic
    ``ValidationError`` translation into ``RequestValidationError``.
    Complex dep graphs and form/file params fall back to the default
    compiled handler since those tutorials don't exercise them.
    """
    import inspect as _ins
    import typing as _tp
    import json as _json
    from pydantic import TypeAdapter as _TA, ValidationError as _PyVE, BaseModel as _BM
    from fastapi_turbo.exceptions import (
        RequestValidationError as _RVE,
        HTTPException as _HE,
    )
    from fastapi_turbo.param_functions import _ParamMarker as _PM
    from fastapi_turbo.dependencies import Depends as _Dep
    from fastapi_turbo.responses import Response as _Resp, JSONResponse as _JR
    from fastapi_turbo.encoders import jsonable_encoder as _je

    endpoint = route.endpoint
    try:
        sig = _ins.signature(endpoint)
    except (TypeError, ValueError):
        sig = None
    try:
        hints = _tp.get_type_hints(endpoint, include_extras=True)
    except Exception as _exc:  # noqa: BLE001
        _log.debug("silent catch in applications: %r", _exc)
        hints = {}

    # Classify params from the endpoint signature.
    # Each entry: (name, kind, annotation, marker, default)
    # kind in {"body", "request", "query", "path", "header", "cookie",
    #          "depends", "other"}.
    param_plan: list[dict] = []
    if sig is not None:
        from fastapi_turbo.requests import Request as _Req, HTTPConnection as _HC
        for pname, p in sig.parameters.items():
            if p.kind in (_ins.Parameter.VAR_POSITIONAL, _ins.Parameter.VAR_KEYWORD):
                continue
            ann = hints.get(pname, p.annotation)
            inner_ann = ann
            markers: list = []
            if _tp.get_origin(ann) is _tp.Annotated:
                args = _tp.get_args(ann)
                if args:
                    inner_ann = args[0]
                    markers = [m for m in args[1:]]
            default = p.default
            marker = None
            for m in markers:
                if isinstance(m, (_PM, _Dep)):
                    marker = m
                    break
            if marker is None and isinstance(default, (_PM, _Dep)):
                marker = default
            kind = "other"
            if isinstance(marker, _Dep):
                kind = "depends"
            elif isinstance(marker, _PM):
                kind = getattr(marker, "_kind", "query") or "query"
            elif isinstance(inner_ann, type) and issubclass(inner_ann, (_Req, _HC)):
                kind = "request"
            elif pname not in getattr(route, "path_params", set()) and _looks_like_body(inner_ann):
                # FA parity: a Pydantic model / dataclass / generic-mapping
                # annotation without an explicit marker is a Body param.
                # Path params are excluded since ``/{x}`` wins.
                kind = "body"
            param_plan.append({
                "name": pname,
                "kind": kind,
                "ann": inner_ann,
                "marker": marker,
                "default": default,
            })

    # Pre-build a body TypeAdapter when a single Body() param exists.
    body_param = next((p for p in param_plan if p["kind"] == "body"), None)
    body_adapter = None
    body_model_cls = None
    if body_param is not None:
        try:
            if isinstance(body_param["ann"], type) and issubclass(body_param["ann"], _BM):
                body_model_cls = body_param["ann"]
            else:
                body_adapter = _TA(body_param["ann"])
        except Exception as _exc:  # noqa: BLE001
            _log.debug("silent catch in applications: %r", _exc)

    is_async_endpoint = _ins.iscoroutinefunction(endpoint)
    _app_ref = app
    _route_ref = route

    async def default_handler(request):
        # Build kwargs one param at a time; for Body, read from
        # ``await request.body()`` so Request subclass overrides fire.
        kwargs: dict = {}
        for p in param_plan:
            nm = p["name"]
            kd = p["kind"]
            if kd == "request":
                kwargs[nm] = request
            elif kd == "body":
                raw = await request.body()
                if not raw:
                    # Missing body → raise RequestValidationError mirroring
                    # FA's "missing required body" response.
                    raise _RVE([
                        {
                            "type": "missing",
                            "loc": ("body",),
                            "msg": "Field required",
                            "input": None,
                        }
                    ], body=None)
                try:
                    parsed = _json.loads(raw)
                except _json.JSONDecodeError as exc:
                    raise _RVE([
                        {
                            "type": "json_invalid",
                            "loc": ("body", exc.pos),
                            "msg": f"JSON decode error: {exc.msg}",
                            "input": {},
                        }
                    ], body=raw.decode("utf-8", errors="replace"))
                try:
                    if body_model_cls is not None:
                        kwargs[nm] = body_model_cls.model_validate(parsed)
                    elif body_adapter is not None:
                        kwargs[nm] = body_adapter.validate_python(parsed)
                    else:
                        kwargs[nm] = parsed
                except _PyVE as exc:
                    # FA parity: prepend ``"body"`` to each error's
                    # ``loc`` tuple and strip the pydantic-only ``url``
                    # / ``ctx`` keys so the JSON response matches FA's
                    # shape exactly (``loc: ["body"]`` for root errors,
                    # ``loc: ["body", "field"]`` for field errors).
                    errs = []
                    for err in exc.errors():
                        # Strip pydantic's doc-link ``url`` field but
                        # preserve ``ctx`` (constraint values like
                        # ``{min_length: 1}``) — upstream FastAPI
                        # emits ctx in 422 response bodies (R39).
                        new_err = {
                            k: v for k, v in err.items()
                            if k != "url"
                        }
                        loc = list(new_err.get("loc", ()))
                        new_err["loc"] = ["body", *loc] if loc else ["body"]
                        errs.append(new_err)
                    raise _RVE(errs, body=parsed) from None
            elif kd == "query":
                # Pull from request.query_params; honour marker's alias/default.
                alias = getattr(p["marker"], "alias", None) or nm
                raw_val = request.query_params.get(alias)
                if raw_val is None:
                    if p["default"] is not _ins.Parameter.empty and not isinstance(p["default"], _PM):
                        kwargs[nm] = p["default"]
                    elif hasattr(p["marker"], "default") and p["marker"].default is not ...:
                        kwargs[nm] = p["marker"].default
                else:
                    kwargs[nm] = raw_val
            elif kd == "path":
                kwargs[nm] = request.path_params.get(nm)
            elif kd == "header":
                alias = getattr(p["marker"], "alias", None) or nm
                kwargs[nm] = request.headers.get(alias.replace("_", "-"))
            elif kd == "cookie":
                kwargs[nm] = request.cookies.get(nm)
            elif kd == "depends":
                # Minimal support: call the dep with no args (sync or async).
                # Tutorial patterns don't exercise deep dep graphs through
                # the custom-route-class path; when someone does, the
                # Rust-compiled pipeline handles it instead.
                dep_fn = p["marker"].dependency
                if dep_fn is None:
                    kwargs[nm] = None
                elif _ins.iscoroutinefunction(dep_fn):
                    kwargs[nm] = await dep_fn()
                else:
                    kwargs[nm] = dep_fn()
            else:  # "other" — primitive query without marker
                kwargs[nm] = request.query_params.get(nm)

        # Call the endpoint.
        result = endpoint(**kwargs)
        if _ins.iscoroutine(result):
            result = await result

        # Wrap into a Response — defer to the route's response_class /
        # status_code if set, else fall back to JSON encoding. Response
        # instances pass through untouched.
        if isinstance(result, _Resp):
            return result

        rm = getattr(_route_ref, "response_model", None)
        if rm is not None:
            try:
                result = _apply_response_model(
                    result, rm,
                    include=getattr(_route_ref, "response_model_include", None),
                    exclude=getattr(_route_ref, "response_model_exclude", None),
                    exclude_unset=getattr(_route_ref, "response_model_exclude_unset", False),
                    exclude_defaults=getattr(_route_ref, "response_model_exclude_defaults", False),
                    exclude_none=getattr(_route_ref, "response_model_exclude_none", False),
                    by_alias=getattr(_route_ref, "response_model_by_alias", True),
                )
            except Exception as _exc:  # noqa: BLE001
                _log.debug("silent catch in applications: %r", _exc)

        response_cls = getattr(_route_ref, "response_class", None) or _JR
        status_code = getattr(_route_ref, "status_code", None) or 200
        encoded = _je(result)
        return response_cls(content=encoded, status_code=status_code)

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
    # call routes through this.
    def _build_default() -> Callable:
        return _build_default_route_handler(route, app)

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
