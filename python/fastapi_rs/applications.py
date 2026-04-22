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
from fastapi_rs.routing import APIRouter, APIRoute


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
        from fastapi_rs.responses import Response as _RespBase
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
    from fastapi_rs.exceptions import ResponseValidationError as _RVE
    if not hasattr(response_model, "model_validate"):
        from pydantic import TypeAdapter, ValidationError as _PyVE
        try:
            ta = TypeAdapter(response_model)
        except Exception:  # noqa: BLE001
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
        from fastapi_rs.responses import Response as _R
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
    from fastapi_rs.responses import JSONResponse as _J
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
        except Exception:  # noqa: BLE001
            pass


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
        return type(route).get_route_handler is not APIRoute.get_route_handler
    except AttributeError:
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
    from fastapi_rs.exceptions import (
        RequestValidationError as _RVE,
        HTTPException as _HE,
    )
    from fastapi_rs.param_functions import _ParamMarker as _PM
    from fastapi_rs.dependencies import Depends as _Dep
    from fastapi_rs.responses import Response as _Resp, JSONResponse as _JR
    from fastapi_rs.encoders import jsonable_encoder as _je

    endpoint = route.endpoint
    try:
        sig = _ins.signature(endpoint)
    except (TypeError, ValueError):
        sig = None
    try:
        hints = _tp.get_type_hints(endpoint, include_extras=True)
    except Exception:  # noqa: BLE001
        hints = {}

    # Classify params from the endpoint signature.
    # Each entry: (name, kind, annotation, marker, default)
    # kind in {"body", "request", "query", "path", "header", "cookie",
    #          "depends", "other"}.
    param_plan: list[dict] = []
    if sig is not None:
        from fastapi_rs.requests import Request as _Req, HTTPConnection as _HC
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
        except Exception:  # noqa: BLE001
            pass

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
                        new_err = {
                            k: v for k, v in err.items()
                            if k not in ("url", "ctx")
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
            except Exception:  # noqa: BLE001
                pass

        response_cls = getattr(_route_ref, "response_class", None) or _JR
        status_code = getattr(_route_ref, "status_code", None) or 200
        encoded = _je(result)
        return response_cls(content=encoded, status_code=status_code)

    return default_handler


def _build_custom_route_handler_endpoint(route, app):
    """Return the endpoint that fastapi-rs registers with Rust when a
    route's APIRoute subclass overrides ``get_route_handler``. The
    endpoint takes a single ``Request`` kwarg (Rust injects it via
    ``inject_request``) and delegates to the user's wrapper.

    On first call, builds ``original_route_handler`` via
    ``_build_default_route_handler`` and caches it on the route so
    subsequent requests reuse it. The user's ``get_route_handler``
    returns their coroutine, which closes over ``original_route_handler``
    and wraps the request before calling it.
    """
    from fastapi_rs.responses import JSONResponse as _JR, Response as _Resp
    from fastapi_rs.exceptions import (
        RequestValidationError as _RVE,
        HTTPException as _HE,
    )

    # Expose the builder so ``APIRoute._default_route_handler`` can
    # resolve it. ``get_route_handler``'s ``super().get_route_handler()``
    # call routes through this.
    def _build_default() -> Callable:
        return _build_default_route_handler(route, app)

    route._fastapi_rs_build_default_handler = _build_default  # type: ignore[attr-defined]

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

    custom_route_endpoint._fastapi_rs_custom_route_class = True  # type: ignore[attr-defined]
    return custom_route_endpoint


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
    path=None,
    route_obj=None,
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
    # Build the endpoint context FA surfaces in
    # ``ValidationException.endpoint_ctx`` — file/line/function/path.
    # Tests assert on ``"get_user" in str(exc)`` which uses this.
    import inspect as _inspect_mod
    _endpoint_ctx: dict = {}
    try:
        _endpoint_ctx["function"] = getattr(endpoint, "__name__", None)
        _endpoint_ctx["file"] = _inspect_mod.getsourcefile(endpoint)
        _endpoint_ctx["line"] = _inspect_mod.getsourcelines(endpoint)[1]
    except (TypeError, OSError):
        pass
    if path is not None:
        _endpoint_ctx["path"] = path

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
    # Container-type coercion map: list[int] → set/frozenset/tuple when
    # the handler's annotation is a sequence type other than ``list``.
    # Rust always produces a ``list`` for repeated query/header params;
    # we wrap it here so handlers that declare ``frozenset[int]`` get
    # deduplicated input that matches FA / Pydantic semantics.
    _container_coerce = {
        p["name"]: p["container_type"]
        for p in params
        if p.get("container_type") and p.get("_is_handler_param")
    }
    _container_ctors = {"set": set, "frozenset": frozenset, "tuple": tuple}

    # For tuple-typed form params, build a TypeAdapter so Pydantic
    # enforces arity + per-element type coercion. ``tuple[int, int]``
    # sent as ``values=1&values=2&values=3`` should 422.
    _tuple_form_adapters: dict = {}
    for _p in params:
        if (
            _p.get("_is_handler_param")
            and _p.get("container_type") == "tuple"
            and _p.get("kind") == "form"
        ):
            _ann = _p.get("_unwrapped_annotation")
            import typing as _tp_local
            if _ann is not None and _tp_local.get_origin(_ann) is tuple:
                _args = _tp_local.get_args(_ann)
                # Fixed-arity tuple (``tuple[int, int]``) — TypeAdapter
                # handles both coercion and arity. Variadic tuples
                # (``tuple[int, ...]``) are left to the ctor below.
                if _args and Ellipsis not in _args:
                    try:
                        from pydantic import TypeAdapter as _TA
                        _tuple_form_adapters[_p["name"]] = _TA(_ann)
                    except Exception:  # noqa: BLE001
                        pass

    def _apply_container_coerce(filtered: dict) -> None:
        if not _container_coerce:
            return
        for _k, _name in _container_coerce.items():
            if _k in filtered:
                # Tuple form with fixed arity: route through Pydantic so
                # both arity and per-element types are enforced.
                _adapter = _tuple_form_adapters.get(_k)
                if _adapter is not None:
                    try:
                        filtered[_k] = _adapter.validate_python(filtered[_k])
                        continue
                    except Exception as _exc:  # noqa: BLE001
                        from pydantic import ValidationError as _PyVE
                        if isinstance(_exc, _PyVE):
                            from fastapi_rs.exceptions import (
                                RequestValidationError as _RVE,
                            )
                            _errs = [
                                {**e, "loc": ("body", _k, *tuple(e.get("loc", ())))}
                                for e in _exc.errors()
                            ]
                            raise _RVE(_errs) from None
                        raise
                _ctor = _container_ctors.get(_name)
                if _ctor is not None and not isinstance(filtered[_k], _ctor):
                    try:
                        filtered[_k] = _ctor(filtered[_k])
                    except TypeError:
                        pass

    # Detect required body/form/file params — these want FA's
    # "collect every missing field in one 422" behaviour, which
    # needs the compiled wrapper path so Rust can defer extraction
    # errors rather than returning on the first missing field.
    _has_form_or_body_params = any(
        p.get("required") and p.get("kind") in ("body", "form", "file")
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
            or _container_coerce
            or _has_form_or_body_params
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
                handler_func = _make_sync_wrapper(handler_func, for_handler=True)

            _app_ref = app

            def _compiled_no_deps(**kwargs):
                # Drain any deferred extraction errors from Rust and
                # surface them as a single 422 — matches FA's "one JSON
                # body listing EVERY missing required field" behaviour
                # even when the handler itself has no dep chain.
                _pending = kwargs.pop("__fastapi_rs_extraction_errors__", None)
                _raw_body_str = kwargs.pop("__fastapi_rs_raw_body_str__", None)
                if _pending is not None:
                    from fastapi_rs.responses import JSONResponse as _JSONResp
                    import json as _json
                    detail = _json.loads(_pending)
                    # FA parity: ``RequestValidationError.body`` holds
                    # the raw JSON body (dict) so custom exception
                    # handlers can inspect what the caller sent.
                    _rve_body = None
                    if _raw_body_str is not None:
                        try:
                            _rve_body = _json.loads(_raw_body_str)
                        except Exception:  # noqa: BLE001
                            _rve_body = _raw_body_str
                    try:
                        from fastapi_rs.exceptions import (
                            RequestValidationError as _RVE,
                        )
                        exc = _RVE(detail, body=_rve_body, endpoint_ctx=_endpoint_ctx)
                        if (
                            _app_ref is not None
                            and _RVE in _app_ref.exception_handlers
                        ):
                            handler_raised = False
                            try:
                                handler_result = _app_ref._invoke_exception_handler_strict(exc)
                            except Exception:  # noqa: BLE001
                                handler_raised = True
                                handler_result = None
                            if handler_raised and _app_ref is not None:
                                _app_ref._captured_server_exceptions.append(exc)
                            if handler_result is not None:
                                return handler_result
                    except ImportError:
                        pass
                    return _JSONResp(
                        content={"detail": detail},
                        status_code=422,
                    )
                try:
                    filtered = {k: kwargs[k] for k in handler_param_names if k in kwargs}
                    # Coerce raw strings to Enum types (FastAPI does this automatically)
                    for _ek, _ecls in _enum_coerce.items():
                        if _ek in filtered and isinstance(filtered[_ek], str):
                            try:
                                filtered[_ek] = _ecls(filtered[_ek])
                            except (ValueError, KeyError):
                                pass
                    try:
                        _apply_container_coerce(filtered)
                    except Exception as _ccexc:
                        from fastapi_rs.exceptions import (
                            RequestValidationError as _RVE2,
                        )
                        if isinstance(_ccexc, _RVE2):
                            from fastapi_rs.responses import (
                                JSONResponse as _JRx,
                            )
                            return _JRx(
                                content={"detail": list(_ccexc.errors())},
                                status_code=422,
                            )
                        raise
                    result = handler_func(**filtered)
                except Exception as exc:
                    _maybe_print_debug_traceback(_app_ref, exc)
                    # Handler-response semantics (Starlette parity):
                    #   - specific exception class handled → NOT re-raised
                    #   - ``Exception`` catch-all handled → still re-raised
                    handler_result = None
                    handler_raised = False
                    if _app_ref is not None and _app_ref.exception_handlers:
                        try:
                            handler_result = _app_ref._invoke_exception_handler(exc)
                        except Exception:
                            handler_raised = True
                    handled_by_specific = False
                    if handler_result is not None and not handler_raised:
                        for exc_cls in (
                            _app_ref.exception_handlers.keys() if _app_ref else []
                        ):
                            if exc_cls is Exception:
                                continue
                            if isinstance(exc_cls, type) and isinstance(exc, exc_cls):
                                handled_by_specific = True
                                break
                    try:
                        from fastapi_rs.exceptions import HTTPException as _HE
                        if (
                            _app_ref is not None
                            and not isinstance(exc, _HE)
                            and not handled_by_specific
                        ):
                            _app_ref._captured_server_exceptions.append(exc)
                    except ImportError:
                        pass
                    if handler_result is not None and not handler_raised:
                        return handler_result
                    raise
                if response_model is not None:
                    try:
                        result = _apply_response_model(
                            result, response_model,
                            include=_rm_include, exclude=_rm_exclude,
                            exclude_unset=_rm_exclude_unset,
                            exclude_defaults=_rm_exclude_defaults,
                            exclude_none=_rm_exclude_none,
                            by_alias=_rm_by_alias,
                            endpoint_ctx=_endpoint_ctx,
                        )
                    except Exception as _rve:  # noqa: BLE001
                        # Route through
                        # ``@app.exception_handler(ResponseValidationError)``
                        # if registered. Capture for TestClient's
                        # ``raise_server_exceptions`` — if user's
                        # handler raises, re-raise that. If no handler,
                        # propagate so the test with ``pytest.raises``
                        # sees the RVE.
                        from fastapi_rs.exceptions import (
                            ResponseValidationError as _RVE2,
                        )
                        if (
                            _app_ref is not None
                            and _RVE2 in _app_ref.exception_handlers
                        ):
                            handler_raised = False
                            try:
                                hdl_result = _app_ref._invoke_exception_handler_strict(_rve)
                            except Exception:  # noqa: BLE001
                                handler_raised = True
                                hdl_result = None
                            if handler_raised and _app_ref is not None:
                                _app_ref._captured_server_exceptions.append(_rve)
                                raise
                            if hdl_result is not None:
                                return hdl_result
                        if _app_ref is not None:
                            _app_ref._captured_server_exceptions.append(_rve)
                        raise
                if _response_class is not None:
                    result = _wrap_response_class(result, _response_class)
                if status_code is not None:
                    result = _apply_status_code(result, status_code)
                # Close UploadFile(s) handed to the handler — Starlette
                # parity. ``test_upload_file_is_closed`` asserts the
                # file is closed after the response is built.
                _close_upload_files(filtered)
                return result

            _compiled_no_deps._fastapi_rs_defers_extraction_errors = True  # type: ignore[attr-defined]
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
            # Propagate the user's `use_cache` flag — when False, the same
            # dep callable is re-invoked for each usage within the request.
            bool(dep.get("use_cache", True)),
            # Accumulated Security(..., scopes=[...]) for this dep — used
            # to populate SecurityScopes kwargs at call time. Tuple of
            # (param_name, [scopes]) or None.
            (
                (dep.get("_security_scopes_param"), dep.get("_security_scopes") or [])
                if dep.get("_security_scopes_param")
                else None
            ),
            # FA 0.120+ scope: ``function`` or ``request``. Used to split
            # teardown ordering — function-scope runs immediately after
            # the handler returns (exceptions from yield-after-yield
            # surface as HTTP errors); request-scope is deferred until
            # after the response (including streaming body) is sent.
            dep.get("_dep_scope") or "request",
        ))

    handler_func = endpoint
    if inspect.iscoroutinefunction(handler_func):
        handler_func = _make_sync_wrapper(handler_func, for_handler=True)

    # Capture app reference for override lookup at call time
    _app = app

    # Cache of introspected override plans (signature params + nested deps).
    # Keyed by id(override_callable). Mini-plan lets us filter kwargs to
    # what the override accepts and resolve any sub-``Depends`` it declares.
    _override_plan_cache: dict[int, dict[str, Any]] = {}

    def _resolve_override_kwargs(
        override_func, original_dk, resolved_env, app_obj, cache_obj
    ):
        """Shape ``dk`` for an override. Filter to params the override
        accepts; for its own ``Depends()`` markers, resolve each sub-dep
        (one level deep — nested overrides propagate through this same
        path on the next call).
        """
        from fastapi_rs.dependencies import Depends as _Dep
        plan = _override_plan_cache.get(id(override_func))
        if plan is None:
            try:
                sig = inspect.signature(override_func)
            except (TypeError, ValueError):
                _override_plan_cache[id(override_func)] = {"accepted": None, "subs": {}, "sig": None}
                return original_dk
            accepted: set[str] = set()
            subs: dict[str, tuple[Any, Any]] = {}
            for pname, param in sig.parameters.items():
                accepted.add(pname)
                default = param.default
                if isinstance(default, _Dep):
                    subs[pname] = (default.dependency, default)
                else:
                    ann = param.annotation
                    import typing as _typ
                    if _typ.get_origin(ann) is _typ.Annotated:
                        for meta in _typ.get_args(ann)[1:]:
                            if isinstance(meta, _Dep):
                                subs[pname] = (meta.dependency, meta)
                                break
            plan = {"accepted": accepted, "subs": subs, "sig": sig}
            _override_plan_cache[id(override_func)] = plan

        accepted = plan["accepted"]
        if accepted is None:
            return original_dk

        dk: dict[str, Any] = {k: v for k, v in original_dk.items() if k in accepted}

        # Pull missing simple-type params (query/header/cookie) from the raw
        # Request — the override may need params the original dep chain
        # never declared (so Rust never extracted them). We accept the
        # Request out of ``resolved_env`` under the synthetic injection
        # name ``__fastapi_rs_override_request__``.
        _req = resolved_env.get("__fastapi_rs_override_request__")
        _sig = plan.get("sig")
        if _req is not None and _sig is not None:
            for pname, param in _sig.parameters.items():
                if pname in dk:
                    continue
                if pname in plan["subs"]:
                    continue
                # Pull from query string first, then headers, then cookies.
                try:
                    qp = _req.query_params
                    if pname in qp:
                        dk[pname] = qp[pname]
                        continue
                except Exception:  # noqa: BLE001
                    pass
                try:
                    hp = _req.headers
                    if pname in hp:
                        dk[pname] = hp[pname]
                        continue
                except Exception:  # noqa: BLE001
                    pass
                try:
                    cp = _req.cookies
                    if pname in cp:
                        dk[pname] = cp[pname]
                        continue
                except Exception:  # noqa: BLE001
                    pass

        for pname, (sub_callable, _sub_marker) in plan["subs"].items():
            # Respect nested dependency_overrides.
            effective = sub_callable
            if app_obj is not None and app_obj.dependency_overrides:
                effective = app_obj.dependency_overrides.get(sub_callable, sub_callable)
            sub_key = id(effective)
            if sub_key in cache_obj:
                dk[pname] = cache_obj[sub_key]
                continue
            # Resolve sub-dep's own kwargs recursively (one level).
            sub_dk = _resolve_override_kwargs(effective, resolved_env, resolved_env, app_obj, cache_obj)
            try:
                if inspect.iscoroutinefunction(effective):
                    sub_val = _make_sync_wrapper(effective)(**sub_dk)
                else:
                    sub_val = effective(**sub_dk)
            except TypeError as te:
                # Override's sub-dep needs request-bound params we never
                # extracted (e.g. a query param ``k`` that the original
                # chain didn't require). Convert to a FA-shaped 422 —
                # identifying missing required params from the
                # signature.
                msg = str(te)
                missing = []
                try:
                    sig = inspect.signature(effective)
                    for pn, p in sig.parameters.items():
                        if pn not in sub_dk and p.default is inspect.Parameter.empty:
                            missing.append(pn)
                except Exception:  # noqa: BLE001
                    pass
                if missing:
                    from fastapi_rs.exceptions import HTTPException as _HE
                    detail = [
                        {
                            "type": "missing",
                            "loc": ["query", m],
                            "msg": "Field required",
                            "input": None,
                        }
                        for m in missing
                    ]
                    raise _HE(status_code=422, detail=detail) from None
                raise
            dk[pname] = sub_val
            cache_obj[sub_key] = sub_val
        return dk

    # When the resolution plan combined multiple body params (handler body
    # + dep body) into one ``_combined_body`` (see ``build_resolution_plan``),
    # the compiled handler needs to split the combined model back into
    # individual named body slots so dep input_maps that reference them by
    # name still resolve.
    _combined_body_split_names: list[str] | None = None
    for _bstep in params:
        if (
            _bstep.get("name") == "_combined_body"
            and _bstep.get("_is_combined_body_for_deps")
            and _bstep.get("_body_param_names")
        ):
            _combined_body_split_names = list(_bstep["_body_param_names"])
            break

    def _compiled(**kwargs):
        # FastAPI semantics: a ``Depends(...)`` that raises
        # ``HTTPException`` short-circuits BEFORE parameter validation
        # errors surface. Rust collects extraction errors and stashes
        # them in a private kwarg so we can try running deps first —
        # if any dep raises ``HTTPException`` that response wins; if
        # all deps succeed we then emit the queued 422.
        _raw_body_str_pending = kwargs.pop("__fastapi_rs_raw_body_str__", None)
        _pending_extraction_errors_json = kwargs.pop(
            "__fastapi_rs_extraction_errors__", None
        )
        # If the plan combined body params for the dep chain, unpack the
        # combined model into the individual names so downstream input_maps
        # still find ``item`` / ``item2`` / etc.
        if _combined_body_split_names is not None:
            _cb = kwargs.get("_combined_body")
            if _cb is not None:
                for _bn in _combined_body_split_names:
                    try:
                        kwargs[_bn] = getattr(_cb, _bn)
                    except AttributeError:
                        pass
        resolved = kwargs
        cache = {}
        generators_to_cleanup: list[tuple] = []
        # Starlette/FastAPI semantics: yield-dep teardown runs AFTER the
        # response has been built and the middleware chain has unwound.
        # If this call originates from a middleware-wrapped entry point,
        # `_middleware_request` is present in kwargs and carries a
        # `_pending_teardowns` list that the outer wrapper drains once
        # the MW chain returns. When no middleware is in play we fall back
        # to running teardown in our own finally block (no deferral
        # possible).
        _mw_req = kwargs.get("_middleware_request")
        _defer_teardown = _mw_req is not None
        if _defer_teardown:
            if not hasattr(_mw_req, "_pending_teardowns"):
                _mw_req._pending_teardowns = []

        try:
            for name, func, original_func, input_map, func_id, is_generator, use_cache, sec_scopes_info, dep_scope in dep_chain:
                # Check dependency_overrides at call time (P0 fix #1)
                actual_func = func
                override_used = None
                if _app is not None and _app.dependency_overrides:
                    override = _app.dependency_overrides.get(original_func)
                    if override is not None:
                        override_used = override
                        actual_func = override
                        if inspect.iscoroutinefunction(actual_func):
                            actual_func = _make_sync_wrapper(actual_func)

                # Respect `use_cache=False` — force a fresh call for each
                # usage of this dep within the request (FastAPI semantics).
                # Cache key also includes ``dep_scope`` — FA 0.120+ treats
                # ``scope="function"`` and ``scope="request"`` as separate
                # instances (so ``function``-scope teardown doesn't tear
                # down a ``request``-scope sibling).
                _cache_key = (func_id, dep_scope) if func_id is not None else None
                if use_cache and _cache_key is not None and _cache_key in cache:
                    resolved[name] = cache[_cache_key]
                    continue
                # Skip deps whose required inputs never arrived — when
                # there are queued extraction errors, a missing input
                # is what the user will see in the 422 anyway. Running
                # the dep body with missing kwargs would just raise a
                # confusing TypeError. But when an override is in play,
                # run it regardless: the override's signature may not
                # need the missing inputs at all (FA parity for
                # ``dependency_overrides``).
                if (
                    override_used is None
                    and _pending_extraction_errors_json is not None
                    and any(sk not in resolved for _, sk in input_map)
                ):
                    continue
                dk = {pn: resolved[sk] for pn, sk in input_map if sk in resolved}

                # Populate this dep's SecurityScopes param with the
                # accumulated ``Security(..., scopes=[...])`` scopes
                # from the call chain. Rust injected an empty
                # SecurityScopes placeholder; we replace it here.
                if sec_scopes_info is not None:
                    _ss_param, _ss_list = sec_scopes_info
                    try:
                        from fastapi_rs.security import SecurityScopes as _SS
                        dk[_ss_param] = _SS(scopes=list(_ss_list))
                    except Exception:  # noqa: BLE001
                        pass

                if override_used is not None:
                    # Override may have a different signature from the
                    # original (fewer params, or its own Depends sub-deps).
                    # Drop kwargs the override doesn't accept, and resolve
                    # its sub-deps via a lazily-built mini-plan.
                    dk = _resolve_override_kwargs(
                        override_used, dk, resolved, _app, cache
                    )

                if is_generator:
                    # Generator dep (yield) support — sync generators drive
                    # via next(); async generators via the shared worker loop
                    # so asyncpg / redis.asyncio connection pools (created on
                    # the worker loop at startup) continue to work. Using a
                    # one-shot loop here would invalidate pool state.
                    gen = actual_func(**dk)
                    if inspect.isasyncgen(gen):
                        from fastapi_rs._async_worker import submit as _submit
                        result = _submit(gen.__anext__())
                        # Sentinel "worker" so teardown knows to route back.
                        generators_to_cleanup.append((gen, "worker", dep_scope))
                    else:
                        result = next(gen)
                        generators_to_cleanup.append((gen, None, dep_scope))
                else:
                    result = actual_func(**dk)

                resolved[name] = result
                if use_cache and _cache_key is not None:
                    cache[_cache_key] = result
        except Exception as dep_exc:
            # Dependency raised — route through exception_handlers like
            # FastAPI/Starlette does. SGLang depends on this (route-level
            # `dependencies=[Depends(...)]` that raise HTTPException).
            _maybe_print_debug_traceback(_app, dep_exc)
            # Capture non-HTTP dep failures so TestClient re-raises them
            # (unless a SPECIFIC handler catches — ``Exception`` catch-all
            # doesn't count, per Starlette's ``raise_server_exceptions``).
            try:
                from fastapi_rs.exceptions import HTTPException as _HE
                _handled_by_specific = False
                if _app is not None and _app.exception_handlers:
                    for _cls in _app.exception_handlers.keys():
                        if _cls is Exception:
                            continue
                        if isinstance(_cls, type) and isinstance(dep_exc, _cls):
                            _handled_by_specific = True
                            break
                if (
                    _app is not None
                    and not isinstance(dep_exc, _HE)
                    and not _handled_by_specific
                ):
                    _app._captured_server_exceptions.append(dep_exc)
            except ImportError:
                pass
            if _app is not None and _app.exception_handlers:
                handler_result = _app._invoke_exception_handler(dep_exc)
                if handler_result is not None:
                    return handler_result
            raise

        # All deps succeeded without raising. If Rust queued any
        # extraction errors (missing headers, unparseable path params,
        # etc.) surface them now — matches FA's post-dep validation
        # order. BUT when dependency_overrides replace deps whose input
        # params are what triggered the 422, the override may have
        # legitimately satisfied the handler — in that case swallow
        # queued errors that don't correspond to a still-missing
        # handler param.
        if _pending_extraction_errors_json is not None:
            from fastapi_rs.responses import JSONResponse as _JSONResp
            import json as _json
            detail = _json.loads(_pending_extraction_errors_json)
            # Filter: keep only errors whose first non-section loc item
            # matches a handler param that's STILL unresolved. If the
            # override made the dep succeed, the error isn't user-visible.
            if _app is not None and _app.dependency_overrides:
                kept = []
                for err in detail:
                    loc = err.get("loc") or []
                    leaf = loc[-1] if loc else None
                    if leaf in handler_param_names and leaf not in resolved:
                        kept.append(err)
                detail = kept
            if detail:
                # Route through ``@app.exception_handler(RequestValidationError)``
                # if registered; otherwise fall back to the default
                # ``{"detail": [...]}`` 422 body. Only capture for
                # ``TestClient`` re-raise when the handler itself raised
                # (matching FA's raise-propagates-out semantic).
                try:
                    from fastapi_rs.exceptions import (
                        RequestValidationError as _RVE,
                    )
                    _rve_body2 = None
                    if _raw_body_str_pending is not None:
                        try:
                            _rve_body2 = _json.loads(_raw_body_str_pending)
                        except Exception:
                            _rve_body2 = _raw_body_str_pending
                    exc = _RVE(detail, body=_rve_body2, endpoint_ctx=_endpoint_ctx)
                    if _app is not None and _RVE in _app.exception_handlers:
                        handler_raised = False
                        try:
                            handler_result = _app._invoke_exception_handler_strict(exc)
                        except Exception:  # noqa: BLE001
                            handler_raised = True
                            handler_result = None
                        if handler_raised and _app is not None:
                            _app._captured_server_exceptions.append(exc)
                        if handler_result is not None:
                            return handler_result
                except ImportError:
                    pass
                return _JSONResp(content={"detail": detail}, status_code=422)

        _raised_exc = None
        _final_result_holder: list = [None]
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
                try:
                    _apply_container_coerce(_hkwargs)
                except Exception as _ccexc2:
                    from fastapi_rs.exceptions import (
                        RequestValidationError as _RVE3,
                    )
                    if isinstance(_ccexc2, _RVE3):
                        from fastapi_rs.responses import (
                            JSONResponse as _JRx2,
                        )
                        return _JRx2(
                            content={"detail": list(_ccexc2.errors())},
                            status_code=422,
                        )
                    raise
                result = handler_func(**_hkwargs)
            except Exception as exc:
                _raised_exc = exc
                # In debug mode, surface the full traceback on non-HTTPException errors.
                _maybe_print_debug_traceback(_app, exc)
                # Route through app's exception_handlers if one is registered.
                # Before handling, run yield-dep teardown with the
                # exception THROWN in — this mirrors FA's behaviour,
                # letting the yield-dep's ``except`` clause observe the
                # error (the test suite asserts on this).
                if generators_to_cleanup:
                    try:
                        _run_pending_teardowns(
                            reversed(generators_to_cleanup), throw_exc=exc
                        )
                    except Exception as _te_exc:
                        # FA parity: yield-dep swallowed the handler's
                        # exception and teardown raised ``FastAPIError``.
                        # Capture & replace ``exc`` so downstream
                        # capture + handler logic sees the FastAPIError.
                        exc = _te_exc
                        _raised_exc = _te_exc
                    generators_to_cleanup.clear()
                # Handler-response semantics, matching Starlette:
                #   - specific exception class handled → NOT re-raised
                #   - ``Exception`` catch-all handled → still re-raised
                #     (Starlette's ``ServerErrorMiddleware`` returns a
                #     response AND bubbles up so TestClient can re-raise)
                handler_result = None
                handler_raised = False
                if _app is not None and _app.exception_handlers:
                    try:
                        handler_result = _app._invoke_exception_handler(exc)
                    except Exception:
                        handler_raised = True
                # ``handled_by_specific`` = matched a handler that is NOT
                # the generic ``Exception`` catch-all.
                handled_by_specific = False
                if handler_result is not None and not handler_raised:
                    for exc_cls in _app.exception_handlers.keys() if _app else []:
                        if exc_cls is Exception:
                            continue
                        if isinstance(exc_cls, type) and isinstance(exc, exc_cls):
                            handled_by_specific = True
                            break
                try:
                    from fastapi_rs.exceptions import HTTPException as _HE
                    if (
                        _app is not None
                        and not isinstance(exc, _HE)
                        and not handled_by_specific
                    ):
                        _app._captured_server_exceptions.append(exc)
                except ImportError:
                    pass
                if handler_result is not None and not handler_raised:
                    return handler_result
                raise exc
            # Apply response_model filtering (P0 fix #5)
            if response_model is not None:
                try:
                    result = _apply_response_model(
                        result, response_model,
                        include=_rm_include, exclude=_rm_exclude,
                        exclude_unset=_rm_exclude_unset,
                        exclude_defaults=_rm_exclude_defaults,
                        exclude_none=_rm_exclude_none,
                        endpoint_ctx=_endpoint_ctx,
                    )
                except Exception as _rve:  # noqa: BLE001
                    from fastapi_rs.exceptions import (
                        ResponseValidationError as _RVE2,
                    )
                    if (
                        _app is not None
                        and _RVE2 in _app.exception_handlers
                    ):
                        handler_raised = False
                        try:
                            hdl_result = _app._invoke_exception_handler_strict(_rve)
                        except Exception:  # noqa: BLE001
                            handler_raised = True
                            hdl_result = None
                        if handler_raised and _app is not None:
                            _app._captured_server_exceptions.append(_rve)
                            raise
                        if hdl_result is not None:
                            return hdl_result
                    if _app is not None:
                        _app._captured_server_exceptions.append(_rve)
                    raise
            # Wrap in response_class if set
            if _response_class is not None:
                result = _wrap_response_class(result, _response_class)
            if status_code is not None:
                result = _apply_status_code(result, status_code)
            _final_result_holder[0] = result
            return result
        finally:
            # Starlette parity: close any UploadFile passed to the handler
            # once the request is done so server-side tests can assert
            # ``.file.closed``. Matches Starlette's ``form.close()`` on
            # ``ExceptionMiddleware``'s ``finally`` block.
            try:
                for _v in list(resolved.values()):
                    if hasattr(_v, "close") and hasattr(_v, "filename"):
                        try:
                            _r = _v.close()
                            if hasattr(_r, "__await__"):
                                try:
                                    _r.send(None)
                                except (StopIteration, Exception):
                                    pass
                        except Exception:
                            pass
                    elif isinstance(_v, list):
                        for _iv in _v:
                            if hasattr(_iv, "close") and hasattr(_iv, "filename"):
                                try:
                                    _r = _iv.close()
                                    if hasattr(_r, "__await__"):
                                        try:
                                            _r.send(None)
                                        except (StopIteration, Exception):
                                            pass
                                except Exception:
                                    pass
            except Exception:  # noqa: BLE001
                pass
            # Starlette/FastAPI semantics: yield-dep teardown runs AFTER
            # the middleware chain unwinds, not before. That lets a
            # middleware body read the state mutated during handler
            # execution — e.g.
            # ``response.headers[x] = state.copy()`` in
            # ``test_dependency_contextmanager`` sees ``"started"`` not
            # ``"completed"``. When middleware is present, we stash the
            # teardowns on the middleware request and the outer wrapper
            # drains them post-response. Otherwise run inline.
            #
            # FA 0.120+ scope split: ``function``-scope teardowns MUST run
            # right here (before the response is finalized) — that way an
            # HTTPException raised after ``yield`` bubbles out as the real
            # response. ``request``-scope teardowns keep the legacy
            # deferred behavior (after streaming body completes).
            _function_scope_tds = [
                t for t in generators_to_cleanup
                if (len(t) == 3 and t[2] == "function")
            ]
            _request_scope_tds = [
                t for t in generators_to_cleanup
                if not (len(t) == 3 and t[2] == "function")
            ]
            # Function-scope always inline (including re-raising any
            # HTTPException from a post-yield statement).
            if _function_scope_tds:
                _run_pending_teardowns(
                    reversed(_function_scope_tds),
                    propagate_exceptions=True,
                )
            if _defer_teardown and _mw_req is not None:
                _mw_req._pending_teardowns.extend(
                    reversed(_request_scope_tds)
                )
            elif _request_scope_tds:
                # FA 0.120+ ``scope="request"`` for a StreamingResponse must
                # defer teardown until AFTER the body iterator is fully
                # consumed (the body peeks at state mutated by the dep).
                from fastapi_rs.responses import StreamingResponse as _SR2
                _final_result = _final_result_holder[0]
                if isinstance(_final_result, _SR2):
                    _orig_iter = _final_result.body_iterator
                    _tds = list(reversed(_request_scope_tds))
                    import inspect as _insp
                    if _insp.isasyncgen(_orig_iter) or hasattr(_orig_iter, "__anext__"):
                        async def _wrap_iter(orig=_orig_iter, tds=_tds):
                            try:
                                async for item in orig:
                                    yield item
                            finally:
                                _run_pending_teardowns(tds)
                        _final_result.body_iterator = _wrap_iter()
                    else:
                        def _wrap_iter_sync(orig=_orig_iter, tds=_tds):
                            try:
                                for item in orig:
                                    yield item
                            finally:
                                _run_pending_teardowns(tds)
                        _final_result.body_iterator = _wrap_iter_sync()
                else:
                    # FA parity: a post-yield ``raise`` in a yield-dep
                    # bubbles up to the TestClient via
                    # ``_captured_server_exceptions``. The handler's
                    # response has already been computed, so collect
                    # (don't raise) any teardown errors.
                    _td_errs: list = []
                    _run_pending_teardowns(
                        reversed(_request_scope_tds),
                        collected_errors=_td_errs,
                    )
                    if _td_errs and _app is not None:
                        for _td_exc in _td_errs:
                            _app._captured_server_exceptions.append(_td_exc)

    # Marker for the Rust router: this compiled handler knows how to
    # consume a deferred extraction-errors blob, so Rust should stash
    # (not raise) 422s and let dep bodies run first — matching FA's
    # "HTTPException from Depends wins over param validation" rule.
    _compiled._fastapi_rs_defers_extraction_errors = True  # type: ignore[attr-defined]

    return _compiled


def _run_pending_teardowns(
    teardowns,
    throw_exc: BaseException | None = None,
    propagate_exceptions: bool = False,
    collected_errors: list | None = None,
) -> None:
    """Drain a reversed-order iterable of (gen, loop[, scope]) tuples.

    Sync yield-deps resume via `next()`; async yield-deps resume on the
    shared worker loop via `_async_worker.submit()` so that asyncpg /
    redis.asyncio teardown (`await session.close()`, `await conn.close()`)
    runs on the same loop that created the connections.

    When ``propagate_exceptions`` is True (FA 0.120+ function-scope deps),
    any ``HTTPException`` raised in a yield-dep's post-yield statement is
    re-raised so the response reflects it. By default (request-scope /
    legacy) such exceptions are swallowed with Starlette's behavior.
    """
    # Throw-aware teardown: when the handler raised, ``throw_exc`` is
    # set and we push it into each generator via ``gen.throw(...)``
    # (or ``gen.athrow(...)`` for async generators) — letting the
    # yield-dep's ``except`` clause observe the error. FA's parity
    # tests assert that a ``try: yield ... except MyError: errors.append
    # (...)`` block runs when the handler raises ``MyError``.
    for tup in teardowns:
        if len(tup) == 3:
            gen, loop, _scope = tup
        else:
            gen, loop = tup
        swallowed_handler_exc = False
        try:
            if loop == "worker":
                from fastapi_rs._async_worker import submit as _submit
                try:
                    if throw_exc is not None and hasattr(gen, "athrow"):
                        _submit(gen.athrow(throw_exc))
                    else:
                        _submit(gen.__anext__())
                    if throw_exc is not None:
                        swallowed_handler_exc = True
                except StopAsyncIteration:
                    if throw_exc is not None:
                        swallowed_handler_exc = True
            elif loop is not None:
                try:
                    if throw_exc is not None and hasattr(gen, "athrow"):
                        loop.run_until_complete(gen.athrow(throw_exc))
                    else:
                        loop.run_until_complete(gen.__anext__())
                    if throw_exc is not None:
                        swallowed_handler_exc = True
                except StopAsyncIteration:
                    if throw_exc is not None:
                        swallowed_handler_exc = True
                finally:
                    loop.close()
            else:
                if throw_exc is not None:
                    gen.throw(throw_exc)
                    swallowed_handler_exc = True
                else:
                    next(gen)
        except StopIteration:
            if throw_exc is not None:
                swallowed_handler_exc = True
        except BaseException as exc:  # noqa: BLE001
            # Teardown-raised errors:
            # - if we threw the original exception in and the generator
            #   re-raised it (or a different one), treat that as the
            #   new "current" exception to propagate
            # - otherwise Starlette's default: swallow and log.
            if throw_exc is not None and exc is not throw_exc:
                # Gen re-raised a different exception — let it surface.
                raise
            # FA 0.120+: ``scope="function"`` wants HTTPException raised
            # from after the ``yield`` to surface as the HTTP response.
            if propagate_exceptions and throw_exc is None:
                raise
            # FA parity: when teardown of a request-scope yield-dep
            # raises post-yield (handler already completed), collect it
            # for the TestClient's ``raise_server_exceptions=True`` path.
            if (
                collected_errors is not None
                and throw_exc is None
                and exc is not throw_exc
            ):
                collected_errors.append(exc)
        # FA parity: when the handler raised and a yield-dep's
        # post-yield ``except`` clause swallows the exception (generator
        # returns normally instead of re-raising), FA raises
        # ``FastAPIError`` with this specific message to flag the
        # broken dependency pattern.
        if swallowed_handler_exc:
            from fastapi_rs.exceptions import FastAPIError as _FE
            raise _FE(
                "No response returned. Either the view returned nothing "
                "or it is raising an exception and a dependency with "
                "yield caught the exception."
            ) from throw_exc


# Imports hoisted to module-level for the hot path (used by wrapped endpoints)
from fastapi_rs.requests import Request as _Request
from fastapi_rs.responses import JSONResponse as _JSONResponse


def _wrap_with_http_middlewares(endpoint, middlewares, app):
    """Wrap a route endpoint with a chain of @app.middleware("http") functions.

    FastAPI/Starlette semantics: the LAST-decorated middleware is the
    OUTERMOST (runs first on request, last on response). Reverse the
    declaration-order list so `middlewares[0]` is outermost after the
    recursive chain-builder.

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
    # FA: reverse declaration order so last-decorated is outermost.
    middlewares = list(reversed(middlewares))

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
            "root_path": getattr(app, "root_path", "") or "",
            "_handler_kwargs": kwargs,
        }

    def _call_handler_sync(kwargs):
        """Run the underlying handler, returning a Response-normalized value."""
        # Keep `_middleware_request` in kwargs (don't pop) so the compiled
        # handler can see it and defer yield-dep teardown onto the MW
        # wrapper's finally block — Starlette's ordering semantics.
        mw_request = kwargs.get("_middleware_request")
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
        # Starlette inserts an `ExceptionMiddleware` layer BETWEEN the user's
        # `@app.middleware("http")` stack and the route itself. That layer
        # turns `HTTPException` / registered exception classes into proper
        # JSON responses before the user MW sees them. Without an equivalent
        # conversion here, our user-MW `except` clauses would catch
        # `HTTPException` and mangle it — diverging from FastAPI.
        # When the endpoint is a RAW user handler (no ``_try_compile_handler``
        # wrap, which happens for no-deps / no-response-model routes), it
        # won't accept our framework-private kwargs. Filter them out.
        _call_kwargs = kwargs
        if not getattr(endpoint, "_has_http_middleware", False) and not getattr(
            endpoint, "_fastapi_rs_defers_extraction_errors", False
        ):
            # Only strip the internal-only keys — every other kwarg is
            # a real handler arg resolved by Rust.
            _PRIVATE = {
                "_middleware_request",
                "__fastapi_rs_extraction_errors__",
                "_request_method",
                "_request_path",
                "_request_query",
                "_request_headers",
            }
            if any(k in kwargs for k in _PRIVATE):
                _call_kwargs = {k: v for k, v in kwargs.items() if k not in _PRIVATE}
        try:
            if is_async_endpoint:
                coro = endpoint(**_call_kwargs)
                try:
                    coro.send(None)
                    # Suspended — fall back
                    coro.close()
                    raise _MiddlewareSuspendedError()
                except StopIteration as e:
                    result = e.value
            else:
                result = endpoint(**_call_kwargs)
        except _MiddlewareSuspendedError:
            raise
        except BaseException as exc:  # noqa: BLE001
            from fastapi_rs.exceptions import HTTPException as _HTTPExc
            if isinstance(exc, _HTTPExc):
                # Build a JSONResponse with the exception's status/detail —
                # matches Starlette's ExceptionMiddleware conversion.
                detail = exc.detail if exc.detail is not None else "Internal Server Error"
                result = _JSONResponse(
                    content={"detail": detail},
                    status_code=exc.status_code,
                    headers=exc.headers,
                )
            elif app is not None and app.exception_handlers:
                handled = app._invoke_exception_handler(exc)
                if handled is None:
                    raise
                result = handled
            else:
                raise
        # Normalize raw handler return values into a ``Response`` before
        # the middleware chain sees them — FA's ExceptionMiddleware does
        # the same, and user middlewares that do
        # ``response.headers[...]`` assume a real Response. Default is
        # ``JSONResponse`` (matching FA's app-level default) so bare
        # strings get JSON-encoded (``"hello"`` not ``hello``).
        if result is None or hasattr(result, "status_code"):
            return result
        return _JSONResponse(content=result)

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
            try:
                return runner(request, kwargs)
            except _MiddlewareSuspendedError:
                # Fallback: drive everything through a fresh event loop
                return _drive_async_fallback(endpoint, middlewares, app, kwargs, is_async_endpoint)
        finally:
            # Drain deferred yield-dep teardowns AFTER the middleware chain
            # has unwound, matching FA's scoping. Middleware bodies see
            # ``state = "started"`` even though teardown would set it to
            # ``"completed"``. If the handler also registered background
            # tasks (real user tasks, not our synthetic teardown), run
            # them HERE inline so user tasks see "started" state
            # too — then run yield-dep teardowns last (FA parity: bg
            # tasks observe pre-teardown state).
            tears = getattr(request, "_pending_teardowns", None)
            if tears:
                from fastapi_rs.background import BackgroundTasks as _BGT
                bg = None
                for v in kwargs.values():
                    if isinstance(v, _BGT):
                        bg = v
                        break
                if bg is not None and bg._tasks:
                    bg.run_sync()
                _run_pending_teardowns(tears)
                request._pending_teardowns = []

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
        # Public FA-compat cache. Populated lazily by ``self.openapi()``;
        # users may assign to it directly (e.g. after augmenting the
        # generated schema in a custom ``app.openapi`` override).
        self.openapi_schema: dict[str, Any] | None = None
        self.servers = servers
        self.terms_of_service = terms_of_service
        self.contact = contact
        self.license_info = license_info
        self.openapi_tags = openapi_tags
        self.lifespan = lifespan
        # Handle deprecated openapi_prefix -> root_path alias (Gap 20).
        # FA parity: uses ``logger.warning`` (not ``warnings.warn``) so
        # it does NOT trip test suites running with
        # ``filterwarnings = ["error"]``.
        if openapi_prefix and not root_path:
            import logging as _log
            _log.getLogger("fastapi").warning(
                '"openapi_prefix" has been deprecated in favor of "root_path", '
                "which follows more closely the ASGI standard, is simpler, and "
                "more automatic. Check the docs at "
                "https://fastapi.tiangolo.com/advanced/sub-applications/"
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
        # Server-side exceptions worth re-raising in the test thread
        # (``ResponseValidationError``, ``FastAPIError``, raw ``ValueError``s
        # raised during request dispatch). ``TestClient`` drains this after
        # every request when ``raise_server_exceptions=True``.
        self._captured_server_exceptions: list[BaseException] = []
        # Separate FIFO for WebSocket server-side exceptions. Drained by
        # ``_WebSocketTestSession.__exit__`` so the expected Starlette
        # pattern of ``with pytest.raises(WebSocketDisconnect): with
        # client.websocket_connect(...):`` works when the server handler
        # raises on client-side close.
        self._ws_server_exceptions: list[BaseException] = []
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
        """Return all collected route objects with their effective paths.

        Matches FastAPI/Starlette: child routers contributed via
        ``include_router(prefix=...)`` surface as APIRoute instances whose
        ``.path`` already reflects the merged prefix (so callers — OpenAPI
        extensions, reverse-lookup helpers, Sentry integrations, etc. —
        see the same strings they'd see on stock FastAPI).
        """
        all_routes = list(self.router.routes)
        for router, include_prefix, _tags, _meta in self._included_routers:
            # `include_router(prefix=...)` stacks on top of the router's
            # own `.prefix` attribute. Both need to appear in the final
            # effective path.
            effective = (include_prefix or "") + (getattr(router, "prefix", "") or "")
            all_routes.extend(self._flatten_child_routes(router, effective))
        return all_routes

    @staticmethod
    def _flatten_child_routes(router, prefix: str) -> list:
        """Walk a child router recursively, yielding clones of each route
        whose path has the cumulative prefix prepended. Clones are shallow
        (we just swap the ``path`` attribute) so the underlying handlers
        and metadata remain shared.
        """
        import copy as _copy

        out: list = []
        cleaned_prefix = prefix or ""

        def _join(parent_prefix: str, child_path: str) -> str:
            if not parent_prefix:
                return child_path
            trailing = child_path.endswith("/") and child_path != "/"
            joined = parent_prefix.rstrip("/") + "/" + child_path.lstrip("/")
            if joined == "":
                return "/"
            if trailing and not joined.endswith("/"):
                joined += "/"
            return joined

        for route in router.routes:
            clone = _copy.copy(route)
            clone.path = _join(cleaned_prefix, getattr(route, "path", ""))
            out.append(clone)

        # Recurse into nested ``router.include_router(...)`` chains — stack
        # the include-prefix AND the child router's own ``.prefix`` on top
        # of whatever prefix we already have.
        nested = getattr(router, "_included_routers", None)
        if nested:
            for entry in nested:
                if len(entry) >= 2:
                    child_router, child_include_prefix = entry[0], entry[1]
                else:
                    continue
                stacked = cleaned_prefix
                for piece in (child_include_prefix or "", getattr(child_router, "prefix", "") or ""):
                    if piece:
                        stacked = stacked.rstrip("/") + "/" + piece.lstrip("/")
                out.extend(FastAPI._flatten_child_routes(child_router, stacked))
        return out

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
        # FA raises when the resulting route would be both ``prefix=""``
        # AND ``path=""`` — the router's own ``prefix`` counts, so a
        # router with ``APIRouter(prefix="/foo")`` and a ``""`` route is
        # fine under ``app.include_router(router)``.
        _router_own_prefix = getattr(router, "prefix", "") or ""
        if not prefix and not _router_own_prefix:
            from fastapi_rs.exceptions import FastAPIError as _FE
            for r in getattr(router, "routes", []):
                if not getattr(r, "path", ""):
                    raise _FE(
                        "Prefix and path cannot be both empty (e.g. "
                        "'' and '')"
                    )
        # If the included router has ``deprecated=True`` on itself, that
        # should surface on every route reachable through this include.
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
        # Mirror every effective sub-route onto ``self.router.routes``
        # as shadow clones so ``app.router.routes`` surfaces the full
        # flattened list (FA/Starlette parity). Shadow copies carry
        # ``_is_included_shadow=True`` so ``_collect_routes_from_router``
        # skips them during the Rust dispatch flatten.
        try:
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

            def _mirror(src_router, pfx: str) -> None:
                for r in getattr(src_router, "routes", []):
                    if getattr(r, "_is_included_shadow", False):
                        continue
                    clone = _copy.copy(r)
                    clone.path = _stack_path(pfx, getattr(r, "path", ""))
                    clone._is_included_shadow = True
                    self.router.routes.append(clone)
                for entry in getattr(src_router, "_included_routers", []):
                    child_router, child_prefix = entry[0], entry[1]
                    nested = _stack_path(
                        _stack_path(pfx, child_prefix or ""),
                        getattr(child_router, "prefix", "") or "",
                    )
                    _mirror(child_router, nested)

            _mirror(router, full_prefix)
        except Exception:  # noqa: BLE001
            pass

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
        """Decorator to register startup/shutdown handlers.

        Deprecated in FA in favor of ``lifespan=`` — emits
        ``DeprecationWarning`` on registration.
        """
        import warnings as _w

        _w.warn(
            "on_event is deprecated, use lifespan event handlers instead.\n\n"
            "Read more about it in the "
            "[FastAPI docs for Lifespan Events]"
            "(https://fastapi.tiangolo.com/advanced/events/).",
            DeprecationWarning,
            stacklevel=2,
        )

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

    def _invoke_exception_handler_strict(self, exc: BaseException):
        """Like ``_invoke_exception_handler`` but LET raised exceptions
        propagate to the caller. FA's user-registered handler can
        ``raise exc`` to signal "don't suppress, pass through to
        TestClient's re-raise path" — and we need to distinguish that
        from the handler returning a response normally.
        """
        handler = self._lookup_exception_handler(exc)
        if handler is None:
            return None
        from fastapi_rs.requests import Request
        request = Request({"type": "http", "app": self})
        if inspect.iscoroutinefunction(handler):
            coro = handler(request, exc)
            try:
                coro.send(None)
            except StopIteration as e:
                return e.value
            coro.close()
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(handler(request, exc))
            finally:
                loop.close()
        return handler(request, exc)

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

    def _wrap_websocket_endpoint(
        self,
        endpoint,
        route_path: str = "",
        extra_dependencies: list | None = None,
    ):
        """Build a thin wrapper around a WebSocket endpoint that
        - attaches ``ws.app`` so handlers can reach ``app.state``,
        - resolves ``Depends(...)`` parameters (incl. sub-deps + yield),
        - validates scalar params via Pydantic TypeAdapter,
        - catches ``WebSocketException`` (before accept → HTTP reject via
          ``ws._reject``; after accept → close with the given code), and
        - invokes the user handler with the right kwargs.

        Captures server-side exceptions onto ``app._ws_server_exceptions``
        so ``TestClient`` can re-raise them on session close — matches
        Starlette/FastAPI TestClient behaviour where a handler raising
        ``WebSocketDisconnect`` on client close propagates out of the
        ``with client.websocket_connect(...)`` block.
        """
        import inspect as _inspect
        from fastapi_rs.dependencies import Depends as _Depends
        from fastapi_rs.websockets import WebSocket as _WebSocket, WebSocketState as _WSState
        from fastapi_rs.exceptions import WebSocketException as _WSExc

        try:
            sig = _inspect.signature(endpoint)
        except (TypeError, ValueError):
            sig = None

        # Resolve stringified annotations (`from __future__ import
        # annotations`) so we can identify the WebSocket parameter by
        # class identity rather than by string name — some handlers
        # pass the WS under different aliases (`websocket`, `conn`…).
        import typing as _typing_mod
        try:
            type_hints = _typing_mod.get_type_hints(endpoint, include_extras=True)
        except Exception:  # noqa: BLE001
            type_hints = {}

        def _is_websocket_annotation(name: str, raw_ann) -> bool:
            ann = type_hints.get(name, raw_ann)
            if ann is _WebSocket:
                return True
            if isinstance(ann, type) and issubclass(ann, _WebSocket):
                return True
            # Fall back to string comparison for deferred-eval
            # annotations that ``get_type_hints`` couldn't resolve
            # (e.g. referenced modules that weren't importable).
            if isinstance(raw_ann, str) and raw_ann in ("WebSocket", "fastapi_rs.websockets.WebSocket"):
                return True
            return False

        from fastapi_rs.param_functions import (
            Query as _Query,
            Header as _Header,
            Cookie as _Cookie,
            _ParamMarker,
        )

        def _extract_marker(annotation, default):
            """Find a Query/Header/Cookie marker on this param either
            via ``Annotated[T, Query()]`` or ``= Query(...)`` default.
            Returns (marker, effective_default_value).
            """
            import typing as _t
            marker = None
            if isinstance(default, _ParamMarker):
                marker = default
            if _t.get_origin(annotation) is _t.Annotated:
                for m in _t.get_args(annotation)[1:]:
                    if isinstance(m, _ParamMarker):
                        marker = m
                        break
            if marker is None:
                return None, None
            return marker, marker.default

        def _extract_depends(annotation, default):
            """Find a ``Depends(...)`` in an ``Annotated[...]`` metadata
            tuple or as the default value."""
            import typing as _t
            if isinstance(default, _Depends):
                return default
            if _t.get_origin(annotation) is _t.Annotated:
                for m in _t.get_args(annotation)[1:]:
                    if isinstance(m, _Depends):
                        return m
            return None

        def _inner_type(annotation):
            """Strip ``Annotated[T, ...]`` to get the underlying type."""
            import typing as _t
            if _t.get_origin(annotation) is _t.Annotated:
                return _t.get_args(annotation)[0]
            return annotation

        def _resolve_ws_scalar_raw(ws, p_name, marker):
            """Pull a query/cookie/header value off the WebSocket scope."""
            alias = marker.alias or p_name
            if isinstance(marker, _Query):
                return ws.query_params.get(alias)
            if isinstance(marker, _Cookie):
                return ws.cookies.get(alias)
            if isinstance(marker, _Header):
                wire = alias
                if getattr(marker, "convert_underscores", True) and "_" in wire:
                    wire = wire.replace("_", "-")
                return ws.headers.get(wire)
            return None

        # Build a cached endpoint context for ValidationException msgs.
        import inspect as _insp_mod
        _ws_endpoint_ctx: dict = {}
        try:
            _ws_endpoint_ctx["function"] = getattr(endpoint, "__name__", None)
            _ws_endpoint_ctx["file"] = _insp_mod.getsourcefile(endpoint)
            _ws_endpoint_ctx["line"] = _insp_mod.getsourcelines(endpoint)[1]
        except (TypeError, OSError):
            pass
        if route_path:
            _ws_endpoint_ctx["path"] = route_path

        def _build_ctx(ws=None):
            """Build endpoint_ctx dict; prefer the route path from the
            matched scope (covers mount-prefixed sub-apps) over the
            static decoration-time path."""
            ctx = dict(_ws_endpoint_ctx)
            if ws is not None:
                try:
                    rt = ws.scope.get("route") if isinstance(ws.scope, dict) else None
                    if rt is not None and getattr(rt, "path", None):
                        ctx["path"] = rt.path
                except Exception:  # noqa: BLE001
                    pass
            return ctx

        def _validate_scalar(val, ann, p_name, kind, ws=None):
            """Validate + coerce ``val`` against ``ann`` using pydantic
            ``TypeAdapter``. On failure raise
            ``WebSocketRequestValidationError`` — routed through app
            exception_handlers when registered, otherwise translated
            into a ``WebSocketException(1008)`` by the outer wrapper."""
            if val is None or ann is _inspect.Parameter.empty or ann is None:
                return val
            from pydantic import TypeAdapter
            try:
                return TypeAdapter(ann).validate_python(val)
            except Exception as exc:
                from fastapi_rs.exceptions import (
                    WebSocketRequestValidationError as _WRVE,
                )
                errors = []
                try:
                    errors = exc.errors()  # Pydantic ValidationError
                except AttributeError:
                    errors = [{
                        "loc": (kind.lower(), p_name),
                        "msg": str(exc),
                        "type": "value_error",
                    }]
                raise _WRVE(errors, endpoint_ctx=_build_ctx(ws)) from exc

        # Extract path parameter names from the route path. Supports both
        # plain ``{name}`` and Starlette-style ``{name:path}`` converter
        # syntax. These are injected as kwargs by the Rust router bridge
        # and must NOT be re-resolved as query/scalar params.
        import re as _re
        path_params_names: set[str] = set()
        if route_path:
            for m in _re.finditer(r"\{([^{}:]+)(?::[^{}]+)?\}", route_path):
                path_params_names.add(m.group(1))

        # Classify every handler parameter up-front.
        # Each entry: ("dep"|"scalar"|"ws"|"path"|"skip", name, meta)
        param_spec: list[tuple] = []
        if sig is not None:
            for name, param in sig.parameters.items():
                default = param.default
                raw_ann = param.annotation
                resolved_ann = type_hints.get(name, raw_ann)

                # Depends (either annotated or as default)
                dep_marker = _extract_depends(resolved_ann, default)
                if dep_marker is not None:
                    if dep_marker.dependency is None:
                        # Blank Depends() — resolve via declared type
                        continue
                    param_spec.append(("dep", name, dep_marker))
                    continue

                if _is_websocket_annotation(name, raw_ann):
                    param_spec.append(("ws", name, None))
                    continue

                # Path param — injected by the router bridge as kwargs.
                if name in path_params_names:
                    param_spec.append(("path", name, _inner_type(resolved_ann)))
                    continue

                marker, _ = _extract_marker(resolved_ann, default)
                if marker is not None:
                    param_spec.append(
                        ("scalar", name, (marker, _inner_type(resolved_ann))),
                    )
                    continue

                # Plain-typed param without marker → Query (FA default for WS).
                # Skip **kwargs/*args/positional-only oddities.
                if param.kind in (
                    _inspect.Parameter.VAR_POSITIONAL,
                    _inspect.Parameter.VAR_KEYWORD,
                ):
                    continue

                # FA treats plain-typed WS params as Query (path params are
                # injected separately by the router bridge).
                if resolved_ann is _inspect.Parameter.empty:
                    # Untyped — best-effort: pass through as Query string.
                    default_val = None if default is _inspect.Parameter.empty else default
                    q = _Query(default=default_val if default_val is not None else ...)
                    param_spec.append(("scalar", name, (q, str)))
                    continue

                default_val = None if default is _inspect.Parameter.empty else default
                from pydantic_core import PydanticUndefined as _PU
                q_default = default_val if default is not _inspect.Parameter.empty else ...
                q = _Query(default=q_default)
                param_spec.append(("scalar", name, (q, resolved_ann)))

        is_async_endpoint = _inspect.iscoroutinefunction(endpoint)
        app_ref = self

        # Build scope-mismatch check at decoration time (FastAPI 0.120+):
        # a ``request``-scope yield dep cannot depend on a ``function``-scope
        # yield dep. Raise ``FastAPIError`` immediately on violation.
        def _get_dep_scope(dep) -> str:
            s = getattr(dep, "scope", None)
            return s if s in ("function", "request") else "request"

        def _check_scope_mismatch(dep: "_Depends", visited: set):
            dep_func = dep.dependency
            if dep_func is None or id(dep_func) in visited:
                return
            visited.add(id(dep_func))
            try:
                dep_sig = _inspect.signature(dep_func)
            except (TypeError, ValueError):
                return
            try:
                dep_hints = _typing_mod.get_type_hints(dep_func, include_extras=True)
            except Exception:  # noqa: BLE001
                dep_hints = {}
            outer_scope = _get_dep_scope(dep)
            outer_is_yield = (
                _inspect.isgeneratorfunction(dep_func)
                or _inspect.isasyncgenfunction(dep_func)
            )
            for p_name, p in dep_sig.parameters.items():
                ann = dep_hints.get(p_name, p.annotation)
                sub = _extract_depends(ann, p.default)
                if sub is None or sub.dependency is None:
                    continue
                sub_scope = _get_dep_scope(sub)
                sub_is_yield = (
                    _inspect.isgeneratorfunction(sub.dependency)
                    or _inspect.isasyncgenfunction(sub.dependency)
                )
                if (
                    outer_is_yield
                    and sub_is_yield
                    and outer_scope == "request"
                    and sub_scope == "function"
                ):
                    from fastapi_rs.exceptions import FastAPIError as _FE
                    outer_name = getattr(dep_func, "__name__", repr(dep_func))
                    raise _FE(
                        f'The dependency "{outer_name}" has a scope of "request", '
                        f'it cannot depend on dependencies with scope "function"'
                    )
                _check_scope_mismatch(sub, visited)

        for kind, _name, meta in param_spec:
            if kind == "dep":
                _check_scope_mismatch(meta, set())
        if extra_dependencies:
            for extra_dep in extra_dependencies:
                if extra_dep is not None and getattr(extra_dep, "dependency", None) is not None:
                    _check_scope_mismatch(extra_dep, set())

        def _effective_dep_callable(dep_callable):
            """Honour ``app.dependency_overrides``."""
            if app_ref is not None and app_ref.dependency_overrides:
                return app_ref.dependency_overrides.get(dep_callable, dep_callable)
            return dep_callable

        async def _call_maybe_async(fn, kwargs):
            """Call ``fn``; await the result if it's a coroutine."""
            r = fn(**kwargs)
            if _inspect.iscoroutine(r):
                return await r
            return r

        async def _resolve_dep_async(dep, ws, generators, cache):
            """Recursively resolve a ``Depends(...)`` chain for the WS
            endpoint. Returns the resolved value. ``generators`` is a
            list of ``(gen, is_async, scope)`` pushed onto by yield-deps
            for later teardown. ``cache`` de-duplicates by dep callable
            when ``use_cache=True``."""
            original = dep.dependency
            effective = _effective_dep_callable(original)
            use_cache = getattr(dep, "use_cache", True)
            cache_key = id(original)
            if use_cache and cache_key in cache:
                return cache[cache_key]

            import typing as _typing
            try:
                dep_sig = _inspect.signature(effective)
            except (TypeError, ValueError):  # noqa: BLE001
                dep_sig = None
            try:
                dep_hints = _typing.get_type_hints(effective, include_extras=True)
            except Exception:  # noqa: BLE001
                dep_hints = {}

            dep_kwargs: dict = {}
            if dep_sig is not None:
                for p_name, p in dep_sig.parameters.items():
                    ann = dep_hints.get(p_name, p.annotation)
                    raw = p.annotation
                    # WebSocket injection
                    if (
                        ann is _WebSocket
                        or (isinstance(ann, type) and issubclass(ann, _WebSocket))
                        or (
                            isinstance(raw, str)
                            and raw in ("WebSocket", "fastapi_rs.websockets.WebSocket")
                        )
                    ):
                        dep_kwargs[p_name] = ws
                        continue
                    # Sub-dependency
                    sub_dep = _extract_depends(ann, p.default)
                    if sub_dep is not None and sub_dep.dependency is not None:
                        dep_kwargs[p_name] = await _resolve_dep_async(
                            sub_dep, ws, generators, cache,
                        )
                        continue
                    # Scalar (Query/Header/Cookie) with validation
                    marker, default_val = _extract_marker(ann, p.default)
                    if marker is not None:
                        raw_val = _resolve_ws_scalar_raw(ws, p_name, marker)
                        if raw_val is None:
                            from pydantic_core import PydanticUndefined as _PU
                            if default_val is not _PU and default_val is not ...:
                                dep_kwargs[p_name] = default_val
                                continue
                            # Missing required scalar → 1008.
                            raise _WSExc(
                                code=1008,
                                reason=f"missing {marker.__class__.__name__} {p_name!r}",
                            )
                        inner = _inner_type(ann)
                        if inner is _inspect.Parameter.empty:
                            dep_kwargs[p_name] = raw_val
                        else:
                            dep_kwargs[p_name] = _validate_scalar(
                                raw_val, inner, p_name,
                                marker.__class__.__name__,
                            )
                        continue

            # Invoke the dependency (sync/async, function/generator).
            scope = _get_dep_scope(dep)
            is_async_gen = _inspect.isasyncgenfunction(effective)
            is_gen = _inspect.isgeneratorfunction(effective)

            if is_async_gen:
                agen = effective(**dep_kwargs)
                value = await agen.__anext__()
                generators.append((agen, True, scope))
            elif is_gen:
                gen = effective(**dep_kwargs)
                value = next(gen)
                generators.append((gen, False, scope))
            elif _inspect.iscoroutinefunction(effective):
                value = await effective(**dep_kwargs)
            else:
                value = effective(**dep_kwargs)

            if use_cache:
                cache[cache_key] = value
            return value

        async def _teardown_generators(generators, scope_filter=None):
            """Run yield-dep teardown in reverse. When ``scope_filter`` is
            set, only teardown generators matching that scope."""
            remaining = []
            # iterate in reverse so innermost teardown first
            for gen, is_async, scope in reversed(generators):
                if scope_filter is not None and scope != scope_filter:
                    remaining.append((gen, is_async, scope))
                    continue
                try:
                    if is_async:
                        try:
                            await gen.__anext__()
                        except StopAsyncIteration:
                            pass
                    else:
                        try:
                            next(gen)
                        except StopIteration:
                            pass
                except Exception:
                    # Teardown errors shouldn't mask primary flow.
                    pass
            # remaining is in reversed order; flip back to original order
            generators[:] = list(reversed(remaining))

        def _handle_ws_exc(ws, exc: _WSExc) -> None:
            # Starlette: pre-accept → reject the HTTP handshake with a
            # non-2xx status; post-accept → close with the WS code.
            code = exc.code if exc.code is not None else 1000
            reason = exc.reason or ""
            # Push a WebSocketDisconnect so the testclient surfaces the
            # ACTUAL close code (e.g. 1008 for POLICY_VIOLATION) rather
            # than the HTTP rejection status (403). Matches FA parity.
            try:
                from fastapi_rs.exceptions import WebSocketDisconnect as _WD
                app_ref._ws_server_exceptions.append(_WD(code=code, reason=reason))
            except Exception:  # noqa: BLE001
                pass
            if ws.application_state == _WSState.CONNECTING:
                ws._reject(403)
                return
            try:
                ws._ws.close(code, reason)
            except Exception:  # noqa: BLE001
                pass

        def _capture_server_exception(exc):
            """Push onto the app's capture queues so TestClient can
            re-raise on session close."""
            try:
                app_ref._ws_server_exceptions.append(exc)
            except Exception:  # noqa: BLE001
                pass

        async def _build_kwargs(ws, path_kwargs):
            """Resolve all handler kwargs. Returns (kwargs, generators).
            Raises on dep failure — caller decides how to surface."""
            kwargs: dict = dict(path_kwargs)
            generators: list = []
            cache: dict = {}
            for kind, name, meta in param_spec:
                if kind == "ws":
                    kwargs[name] = ws
                elif kind == "path":
                    # Path params are injected by the router. Validate
                    # via TypeAdapter if a non-str type was declared.
                    val = path_kwargs.get(name)
                    if val is not None and meta is not _inspect.Parameter.empty and meta is not str:
                        kwargs[name] = _validate_scalar(val, meta, name, "Path", ws=ws)
                    else:
                        kwargs[name] = val
                elif kind == "scalar":
                    marker, inner = meta
                    raw_val = _resolve_ws_scalar_raw(ws, name, marker)
                    if raw_val is None:
                        default_val = marker.default
                        from pydantic_core import PydanticUndefined as _PU
                        if default_val is _PU or default_val is ...:
                            # Required — 1008.
                            raise _WSExc(
                                code=1008,
                                reason=f"missing {marker.__class__.__name__} {name!r}",
                            )
                        kwargs[name] = default_val
                        continue
                    if inner is _inspect.Parameter.empty:
                        kwargs[name] = raw_val
                    else:
                        kwargs[name] = _validate_scalar(
                            raw_val, inner, name,
                            marker.__class__.__name__, ws=ws,
                        )
                elif kind == "dep":
                    kwargs[name] = await _resolve_dep_async(
                        meta, ws, generators, cache,
                    )
            # Resolve extra (app/router/include/route-level) dependencies
            # AFTER handler params are satisfied. Their values aren't
            # bound to a kwarg — run for side-effects only (matches FA:
            # these deps run but their return value is discarded).
            if extra_dependencies:
                for extra_dep in extra_dependencies:
                    if extra_dep is None or getattr(extra_dep, "dependency", None) is None:
                        continue
                    await _resolve_dep_async(extra_dep, ws, generators, cache)
            return kwargs, generators

        # Build a synthetic route object for ``ws.scope["route"]``. FA
        # exposes the matched ``APIWebSocketRoute`` here; third-party
        # code (e.g. route introspection in handlers) uses it to pull
        # the path template.
        try:
            from fastapi_rs.compat import fastapi_shim as _fa_shim
            _APIWSRoute = getattr(
                getattr(_fa_shim, "fastapi_routing", None) or object(),
                "APIWebSocketRoute",
                None,
            )
        except Exception:  # noqa: BLE001
            _APIWSRoute = None
        if _APIWSRoute is None:
            class _APIWSRoute:  # type: ignore[no-redef]
                def __init__(self, path, endpoint, name=None):
                    self.path = path
                    self.endpoint = endpoint
                    self.name = name or getattr(endpoint, "__name__", "")
        _synthetic_route = _APIWSRoute(
            path=route_path,
            endpoint=endpoint,
            name=getattr(endpoint, "__name__", ""),
        )

        async def _ws_entry(ws, **path_kwargs):
            ws._app = app_ref
            # Inject ``route`` into the ASGI-style scope dict.
            try:
                scope = ws.scope
                if isinstance(scope, dict):
                    scope["route"] = _synthetic_route
                    scope["app"] = app_ref
            except Exception:  # noqa: BLE001
                pass
            generators: list = []
            try:
                kwargs, generators = await _build_kwargs(ws, path_kwargs)
                if is_async_endpoint:
                    await endpoint(**kwargs)
                else:
                    endpoint(**kwargs)
            except _WSExc as exc:
                _handle_ws_exc(ws, exc)
                # Run teardown even on exception so yield-deps release
                # resources.
                await _teardown_generators(generators)
                return
            except Exception as exc:
                # Route WebSocketRequestValidationError through the app's
                # exception handlers if registered. FA parity:
                # @app.exception_handler(WebSocketRequestValidationError)
                # receives the validation error; re-raise reaches here.
                try:
                    from fastapi_rs.exceptions import (
                        WebSocketRequestValidationError as _WRVE,
                    )
                except ImportError:
                    _WRVE = None
                if (
                    _WRVE is not None
                    and isinstance(exc, _WRVE)
                    and app_ref is not None
                    and getattr(app_ref, "exception_handlers", None)
                ):
                    # Capture first so tests checking the exc object see it
                    # even when the handler re-raises.
                    _capture_server_exception(exc)
                    handler = app_ref.exception_handlers.get(_WRVE)
                    if handler is not None:
                        try:
                            r = handler(ws, exc)
                            if _inspect.iscoroutine(r):
                                await r
                        except Exception:  # noqa: BLE001
                            pass
                    # Close with 1008 policy-violation regardless of what
                    # the handler did.
                    try:
                        from fastapi_rs.exceptions import (
                            WebSocketDisconnect as _WD,
                        )
                        app_ref._ws_server_exceptions.append(
                            _WD(code=1008, reason="validation error")
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    if ws.application_state == _WSState.CONNECTING:
                        ws._reject(403)
                    else:
                        try:
                            ws._ws.close(1008, "validation error")
                        except Exception:  # noqa: BLE001
                            pass
                    await _teardown_generators(generators)
                    return
                # Route through app-registered exception_handlers if
                # one matches this exception type. FA parity: a handler
                # registered on ``@app.exception_handler(MyError)`` for
                # WebSocket routes runs with ``(websocket, exc)`` and
                # is expected to call ``websocket.close(code, reason)``
                # itself. If it does, the client sees that close code.
                handled = False
                if app_ref is not None and getattr(app_ref, "exception_handlers", None):
                    handler_cls = type(exc)
                    handler = None
                    for k, v in app_ref.exception_handlers.items():
                        try:
                            if isinstance(exc, k):
                                handler = v
                                handler_cls = k
                                break
                        except TypeError:
                            continue
                    if handler is not None:
                        try:
                            r = handler(ws, exc)
                            if _inspect.iscoroutine(r):
                                await r
                            handled = True
                            # Push a disconnect so TestClient surfaces
                            # the WS close code. The handler will have
                            # already called ``ws.close(...)`` but our
                            # testclient runs the client in the same
                            # test thread and can't observe the close
                            # frame after the ``__exit__`` hook — so we
                            # explicitly raise from the capture queue.
                            try:
                                from fastapi_rs.exceptions import (
                                    WebSocketDisconnect as _WD,
                                )
                                last = getattr(ws, "_last_close_code", None) or 1000
                                last_reason = getattr(ws, "_last_close_reason", "") or ""
                                app_ref._ws_server_exceptions.append(
                                    _WD(code=last, reason=last_reason)
                                )
                            except Exception:  # noqa: BLE001
                                pass
                        except Exception:  # noqa: BLE001
                            pass
                # Capture for TestClient re-raise semantics BEFORE we
                # disturb the WS state.
                if not handled:
                    _capture_server_exception(exc)
                if not handled:
                    # Abort the handshake cleanly if still pre-accept so the
                    # client sees an HTTP 500 instead of hanging.
                    if ws.application_state == _WSState.CONNECTING:
                        ws._reject(500)
                    else:
                        # Post-accept unhandled exception — close cleanly so
                        # the client's ``recv()`` sees a close frame.
                        try:
                            ws._ws.close(1006, "")
                        except Exception:  # noqa: BLE001
                            pass
                await _teardown_generators(generators)
                return
            # Normal exit — drain teardowns (both scopes; no response
            # body to stream for WS).
            await _teardown_generators(generators)

        # Expose the synthetic route + endpoint_ctx as attributes so
        # that mount-prefixing can patch the path once the full URL
        # is known (mounted sub-apps are collected with an inner path).
        _ws_entry._ws_synthetic_route = _synthetic_route  # type: ignore[attr-defined]
        _ws_entry._ws_endpoint_ctx = _ws_endpoint_ctx  # type: ignore[attr-defined]

        # Always return an async entry: Rust treats both sync/async the
        # same way via the worker loop, and this lets us await deps and
        # teardown uniformly even for sync endpoints.
        return _ws_entry

    def _get_all_dependencies_for_route(
        self, router: APIRouter, route, include_deps: list | None = None,
    ) -> list:
        """Merge app-level, include-level, router-level, and route-level dependencies."""
        # FA parity: the ``/openapi.json`` / ``/docs`` endpoints bypass
        # ALL user-registered dependencies — the docs should never
        # require app-level auth headers to fetch the schema.
        if getattr(route, "_fastapi_rs_bypass_deps", False):
            return []
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

    def _apply_generate_unique_id(
        self,
        route,
        include_fn: Callable | None,
        router: APIRouter,
    ) -> str | None:
        """FA's operationId cascade: route → router → include → app.

        The router's own ``generate_unique_id_function`` takes
        precedence over an ``include_router(..., generate_unique_id_function
        =...)`` override — matches FA's resolution order.
        """
        fn = (
            getattr(route, "generate_unique_id_function", None)
            or getattr(router, "generate_unique_id_function", None)
            or include_fn
            or getattr(self, "generate_unique_id_function", None)
        )
        if fn is None:
            return None
        # FA parity: when users write ``generate_unique_id_function=
        # Default(my_fn)``, the value is wrapped in a ``DefaultPlaceholder``.
        # Unwrap here before invoking.
        from fastapi_rs.datastructures import DefaultPlaceholder as _DP
        if isinstance(fn, _DP):
            fn = fn.value
        if fn is None or not callable(fn):
            return None
        # Skip internal routes (docs, openapi.json) — user's
        # ``generate_unique_id_function`` likely reads
        # ``route.tags[0]`` and our internal routes have no tags.
        if not getattr(route, "include_in_schema", True):
            return None
        try:
            return fn(route)
        except TypeError:
            return fn(route, (route.methods or ["GET"])[0].lower())

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
        include_generate_unique_id_function: Callable | None = None,
        include_callbacks: list | None = None,
    ) -> list[dict[str, Any]]:
        """Recursively flatten a router tree into a list of route dicts."""
        extra_tags = extra_tags or []
        include_deps = include_deps or []
        include_responses = include_responses or {}
        include_callbacks = include_callbacks or []
        # Router-level ``APIRouter(callbacks=...)`` propagates to every
        # route inside it, stacked on top of outer ``include_callbacks``.
        effective_callbacks = list(include_callbacks) + list(
            getattr(router, "callbacks", []) or []
        )
        collected: list[dict[str, Any]] = []

        full_prefix = prefix + router.prefix

        # Merge the router's own tags into extra_tags so all routes
        # within this router inherit them (FastAPI parity).
        if router.tags:
            extra_tags = extra_tags + router.tags

        for route in router.routes:
            # Shadow copies mirrored into ``self.routes`` by
            # ``include_router(...)`` exist only so ``app.router.routes``
            # surfaces the full flattened list. The real dispatch comes
            # from the child router's ``_included_routers`` entry that we
            # walk below, so skip the shadows here to avoid registering
            # the same path twice.
            if getattr(route, "_is_included_shadow", False):
                continue
            full_path = full_prefix + route.path
            # Normalise accidental double-slash at a join point (e.g.
            # prefix="/api/" + route="/items") without losing a trailing
            # slash that the user declared on purpose — FastAPI/Starlette
            # treat `/items` and `/items/` as distinct routes, and the
            # redirect-slashes middleware depends on that distinction.
            if full_path != "/":
                had_trailing = full_path.endswith("/")
                full_path = "/" + full_path.strip("/")
                if had_trailing:
                    full_path += "/"

            is_websocket = getattr(route, "_is_websocket", False)

            if is_websocket:
                # WebSocket endpoints accept the WebSocket object (always
                # positional) plus optional ``Depends(...)`` parameters.
                # Rust only injects the WS + path params, so we wrap the
                # user's handler to resolve deps BEFORE the user code runs.
                # A pre-accept ``WebSocketException`` aborts the handshake
                # with the carried code (Starlette normative path).
                # Merge extra dependencies from app/router/include/route so
                # test_ws_dependencies patterns (dependencies=[...] on
                # FastAPI(), APIRouter(), include_router(), @ws()) all run.
                merged_ws_deps = self._get_all_dependencies_for_route(
                    router, route, include_deps=include_deps,
                )
                wrapped_ws = self._wrap_websocket_endpoint(
                    route.endpoint, full_path, extra_dependencies=merged_ws_deps,
                )
                collected.append(
                    {
                        "path": full_path,
                        "methods": ["GET"],
                        "endpoint": wrapped_ws,
                        "is_async": inspect.iscoroutinefunction(wrapped_ws),
                        "handler_name": route.name,
                        "tags": extra_tags + route.tags,
                        "params": [],
                        "is_websocket": True,
                    }
                )
                continue

            # ── Custom ``APIRoute`` subclass (GzipRoute, TimedRoute, …) ──
            # When ``type(route).get_route_handler`` is overridden, the
            # user's wrapper runs the request pipeline at the Python
            # layer — Rust just needs to hand the ``Request`` over to a
            # thin adapter. Short-circuit the normal param introspection
            # / compile pipeline so body parsing, validation, and
            # response wrapping all happen inside the user's wrapper
            # (via ``super().get_route_handler()``).
            if _has_overridden_get_route_handler(route):
                custom_ep = _build_custom_route_handler_endpoint(route, self)
                try:
                    custom_ep._fastapi_rs_route_obj = route  # type: ignore[attr-defined]
                except (AttributeError, TypeError):
                    pass
                custom_params = [{
                    "name": "request",
                    "kind": "inject_request",
                    "type_hint": "any",
                    "required": False,
                    "default_value": None,
                    "has_default": True,
                    "model_class": None,
                    "alias": None,
                    "_embed": False,
                    "media_type": None,
                    "example": None,
                    "examples": None,
                    "openapi_examples": None,
                    "title": None,
                    "description": None,
                    "include_in_schema": False,
                    "deprecated": None,
                    "scalar_validator": None,
                    "enum_class": None,
                    "container_type": None,
                    "_is_optional": True,
                    "_enum_values": None,
                    "_unwrapped_annotation": None,
                    "_raw_marker": None,
                    "_raw_annotation": None,
                    "_is_handler_param": True,
                }]
                collected.append({
                    "path": full_path,
                    "methods": route.methods,
                    "endpoint": custom_ep,
                    "is_async": True,
                    "handler_name": route.name,
                    "tags": extra_tags + route.tags,
                    "params": custom_params,
                    "_all_params": list(
                        introspect_endpoint(route.endpoint, full_path)
                    ),
                    "is_websocket": False,
                    "status_code": route.status_code or 200,
                    "summary": route.summary,
                    "description": route.description,
                    "response_description": getattr(route, "response_description", "Successful Response"),
                    "responses": {
                        **self.responses,
                        **include_responses,
                        **getattr(router, "responses", {}),
                        **getattr(route, "responses", {}),
                    },
                    "response_model": getattr(route, "response_model", None),
                    "response_class": getattr(route, "response_class", None),
                    "deprecated": (
                        route.deprecated
                        or bool(getattr(router, "deprecated", False))
                        or bool(include_deprecated)
                    ),
                    "operation_id": (
                        route.operation_id
                        or self._apply_generate_unique_id(
                            route,
                            include_generate_unique_id_function,
                            router,
                        )
                    ),
                    "include_in_schema": (
                        getattr(route, "include_in_schema", True) and include_in_schema
                    ),
                    "openapi_extra": getattr(route, "openapi_extra", {}),
                    "security": getattr(route, "security", None),
                    "callbacks": list(effective_callbacks) + list(
                        getattr(route, "callbacks", []) or []
                    ),
                    "servers": getattr(route, "servers", None),
                    "external_docs": getattr(route, "external_docs", None),
                })
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

            # When the route has deps, inject a hidden Request param so the
            # compiled handler can pull extra query/header values on demand —
            # needed when ``dependency_overrides`` installs a replacement whose
            # own sub-deps depend on params the original chain never declared
            # (FA parity for ``test_dependency_overrides``). Only add if the
            # route doesn't already expose a Request to the user.
            if has_deps and not any(
                p.get("kind") == "inject_request" for p in params
            ):
                params.append({
                    "name": "__fastapi_rs_override_request__",
                    "kind": "inject_request",
                    "type_hint": "any",
                    "required": False,
                    "default_value": None,
                    "has_default": True,
                    "model_class": None,
                    "alias": None,
                    "_embed": False,
                    "media_type": None,
                    "example": None,
                    "examples": None,
                    "openapi_examples": None,
                    "title": None,
                    "description": None,
                    "include_in_schema": False,
                    "deprecated": None,
                    "scalar_validator": None,
                    "enum_class": None,
                    "container_type": None,
                    "_is_optional": True,
                    "_enum_values": None,
                    "_unwrapped_annotation": None,
                    "_raw_marker": None,
                    "_raw_annotation": None,
                    "_is_handler_param": False,
                })

            # Save all params (including deps) for OpenAPI security scheme detection
            all_params_for_openapi = list(params)

            endpoint = route.endpoint

            # FA 0.136+: handlers that are (async) generator functions
            # auto-wrap into a StreamingResponse with JSON-lines content
            # type. Encode each yielded item via jsonable_encoder so
            # BaseModels / dataclasses / bytes / datetimes serialize
            # correctly. Runs BEFORE dep compilation so downstream
            # wrappers see a plain sync callable.
            if (
                inspect.isasyncgenfunction(endpoint)
                or inspect.isgeneratorfunction(endpoint)
            ) and not getattr(route, "response_class", None):
                _orig_endpoint = endpoint
                _is_async_gen = inspect.isasyncgenfunction(endpoint)
                # FA parity: when the return annotation is
                # ``AsyncIterable[Item]`` / ``Iterable[Item]``, validate
                # each yielded item against ``Item`` and raise
                # ``ResponseValidationError`` on mismatch — mirrors real
                # FA's streaming validation path.
                _item_adapter = None
                _rm = getattr(route, "response_model", None)
                import typing as _tp
                import collections.abc as _cabc
                if _tp.get_origin(_rm) in {
                    _cabc.AsyncIterable, _cabc.AsyncIterator,
                    _cabc.AsyncGenerator, _cabc.Iterable,
                    _cabc.Iterator, _cabc.Generator,
                }:
                    _args = _tp.get_args(_rm)
                    if _args:
                        try:
                            from pydantic import TypeAdapter as _TA
                            _item_adapter = _TA(_args[0])
                        except Exception:  # noqa: BLE001
                            _item_adapter = None

                _app_for_stream = self

                def _json_lines_wrap(
                    _orig=_orig_endpoint, _is_a=_is_async_gen,
                    _ta=_item_adapter, _app=_app_for_stream, **kwargs,
                ):
                    from fastapi_rs.responses import StreamingResponse as _SR
                    from fastapi_rs.encoders import jsonable_encoder as _je
                    from fastapi_rs.exceptions import (
                        ResponseValidationError as _RVE,
                    )
                    import json as _json
                    def _check(item):
                        if _ta is None:
                            return item
                        try:
                            return _ta.validate_python(item)
                        except Exception as exc:  # noqa: BLE001
                            from pydantic import ValidationError as _PyVE
                            if isinstance(exc, _PyVE):
                                raise _RVE(errors=exc.errors(), body=item) from None
                            raise
                    if _is_a:
                        async def _iter_async():
                            try:
                                async for item in _orig(**kwargs):
                                    validated = _check(item)
                                    yield (_json.dumps(_je(validated)) + "\n").encode("utf-8")
                            except _RVE as exc:
                                # FA parity: surface streaming-body
                                # validation errors through
                                # ``app._captured_server_exceptions``
                                # so TestClient re-raises with
                                # ``raise_server_exceptions=True``.
                                if _app is not None:
                                    _app._captured_server_exceptions.append(exc)
                                return
                        return _SR(_iter_async(), media_type="application/jsonl")
                    else:
                        def _iter_sync():
                            try:
                                for item in _orig(**kwargs):
                                    validated = _check(item)
                                    yield (_json.dumps(_je(validated)) + "\n").encode("utf-8")
                            except _RVE as exc:
                                if _app is not None:
                                    _app._captured_server_exceptions.append(exc)
                                return
                        return _SR(_iter_sync(), media_type="application/jsonl")

                endpoint = _json_lines_wrap

            # Detect @wraps-wrapped async endpoints — a sync wrapper over an
            # ``async def`` reports ``iscoroutinefunction = False`` but calling
            # it returns a coroutine. Treat those as async so we drive them
            # correctly.
            _raw_async = inspect.iscoroutinefunction(endpoint)
            _wrapped_async = (not _raw_async) and _is_async_callable(endpoint)
            is_async = _raw_async or _wrapped_async
            if _wrapped_async and not inspect.isasyncgenfunction(endpoint):
                # Wrap so calling this endpoint returns a proper coroutine
                # that awaits the inner coroutine (instead of returning one).
                _user_ep = endpoint

                async def _await_wrapped(_ep=_user_ep, **kwargs):
                    result = _ep(**kwargs)
                    if inspect.iscoroutine(result):
                        return await result
                    return result

                endpoint = _await_wrapped
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
                    path=full_path,
                    route_obj=route,
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
                        endpoint = _make_sync_wrapper(endpoint, for_handler=True)
                        is_async = False
            elif (
                response_model is not None
                or response_class is not None
                or route.status_code
                or self.exception_handlers
                or self.debug
                or any(p.get("enum_class") is not None for p in params)
                or any(p.get("container_type") is not None for p in params)
                or any(
                    p.get("required") and p.get("kind") in ("body", "form", "file")
                    for p in params
                )
            ):
                # No deps but has response_model/response_class/status_code/
                # exception_handlers/enum params — wrap handler via compile.
                compiled = _try_compile_handler(
                    endpoint, params, app=self, response_model=response_model,
                    status_code=route.status_code,
                    path=full_path,
                    route_obj=route,
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
                        kwargs.pop("__fastapi_rs_override_request__", None)
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
                        kwargs.pop("__fastapi_rs_override_request__", None)
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

            # FA 0.120+ ``strict_content_type=False`` — closest-wins
            # precedence: route → router → app. A strict inner router
            # overrides a lax app.
            _route_strict = getattr(route, "strict_content_type", None)
            _router_strict = getattr(router, "strict_content_type", None)
            if _route_strict is not None:
                _strict_effective = _route_strict
            elif _router_strict is not None:
                _strict_effective = _router_strict
            else:
                _strict_effective = self.strict_content_type
            _lax = _strict_effective is False
            if _lax:
                try:
                    endpoint._fastapi_rs_lax_content_type = True  # type: ignore[attr-defined]
                except (AttributeError, TypeError):
                    pass
            # Attach the original route object so Rust can populate
            # ``request.scope["route"]`` — ``test_route_scope`` asserts.
            try:
                endpoint._fastapi_rs_route_obj = route  # type: ignore[attr-defined]
            except (AttributeError, TypeError):
                pass
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
                    "response_class": response_class,
                    "deprecated": (
                        route.deprecated
                        or bool(getattr(router, "deprecated", False))
                        or bool(include_deprecated)
                    ),
                    # operation_id cascade: route's own wins, then the
                    # route's explicit generate_unique_id_function, then
                    # include-level, then router-level, then app-level.
                    # Matches FA's
                    # ``operation_id or current_generate_unique_id(self)``.
                    "operation_id": (
                        route.operation_id
                        or self._apply_generate_unique_id(
                            route,
                            include_generate_unique_id_function,
                            router,
                        )
                    ),
                    "include_in_schema": (
                        getattr(route, "include_in_schema", True) and include_in_schema
                    ),
                    "openapi_extra": getattr(route, "openapi_extra", {}),
                    "security": getattr(route, "security", None),
                    "callbacks": list(effective_callbacks) + list(
                        getattr(route, "callbacks", []) or []
                    ),
                    "servers": getattr(route, "servers", None),
                    "external_docs": getattr(route, "external_docs", None),
                }
            )

        # Recurse into child routers — CASCADE include-level metadata
        # down the chain. FA's parity tests expect x-level1 / x-level2 /
        # x-level3 dep headers on deeply nested routes, which requires
        # that an ancestor ``include_router(dependencies=[...])`` apply
        # to descendant routes. Accumulate deps / responses / tags;
        # take the nearest non-None for deprecated / default_response_class.
        for child_router, child_prefix, child_tags, child_meta in router._included_routers:
            # Parent router's own dependencies / responses cascade into
            # descendant routes, same as FA's eager flatten.
            merged_deps = (
                list(include_deps)
                + list(getattr(router, "dependencies", []) or [])
                + list(child_meta.get("dependencies", []) or [])
            )
            merged_resp = {
                **(include_responses or {}),
                **(getattr(router, "responses", {}) or {}),
                **(child_meta.get("responses", {}) or {}),
            }
            child_deprecated = child_meta.get("deprecated")
            effective_deprecated = (
                child_deprecated if child_deprecated is not None else include_deprecated
            )
            # Cascade: child_include_drc → parent router drc → outer include drc.
            # Matches FA's ``get_value_or_default(route.response_class,
            # router.default_response_class, default_response_class,
            # self.default_response_class)`` evaluated recursively as each
            # nested include runs.
            child_drc = child_meta.get("default_response_class")
            if child_drc is None:
                child_drc = getattr(router, "default_response_class", None)
            if child_drc is None:
                child_drc = include_default_response_class
            effective_drc = child_drc
            effective_in_schema = (
                include_in_schema
                and child_meta.get("include_in_schema", True)
            )
            # Propagate ``generate_unique_id_function`` down the chain.
            # Precedence: child's include-arg → parent router's own →
            # outer include arg.
            child_gfn = child_meta.get("generate_unique_id_function")
            if child_gfn is None:
                child_gfn = getattr(router, "generate_unique_id_function", None)
            if child_gfn is None:
                child_gfn = include_generate_unique_id_function
            # Callbacks cascade too: accumulate outer ``effective_callbacks``
            # (which already folded in this router's own callbacks) with
            # the child include's own ``callbacks=`` list so descendant
            # routes inherit them.
            merged_callbacks = (
                list(effective_callbacks)
                + list(child_meta.get("callbacks", []) or [])
            )
            collected.extend(
                self._collect_routes_from_router(
                    child_router,
                    prefix=full_prefix + child_prefix,
                    extra_tags=extra_tags + child_tags,
                    include_deps=merged_deps,
                    include_responses=merged_resp,
                    include_deprecated=effective_deprecated,
                    include_in_schema=effective_in_schema,
                    include_default_response_class=effective_drc,
                    include_generate_unique_id_function=child_gfn,
                    include_callbacks=merged_callbacks,
                )
            )

        return collected

    def _collect_all_routes(self) -> list[dict[str, Any]]:
        """Walk the root router and all included routers, returning a flat list."""
        # App-level callbacks propagate to every top-level route's
        # ``operation.callbacks`` — same as route-level/include-level.
        _app_callbacks = list(getattr(self, "callbacks", []) or [])
        # Routes registered directly on self.router
        all_routes = self._collect_routes_from_router(
            self.router,
            include_callbacks=_app_callbacks,
        )

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
                    include_generate_unique_id_function=meta.get("generate_unique_id_function"),
                    include_callbacks=_app_callbacks + list(meta.get("callbacks") or []),
                )
            )

        # Mounted sub-applications
        for mount_path, mounted_app, _name in self._mounts:
            if isinstance(mounted_app, FastAPI):
                # Collect routes from the mounted FastAPI app with prefix.
                # Mark them with `_from_mount` so the main app's OpenAPI
                # schema can exclude them — Starlette/FastAPI treat a
                # mounted FastAPI as an isolated sub-app whose schema is
                # served at `<mount>/openapi.json`.
                sub_routes = mounted_app._collect_all_routes()
                for r in sub_routes:
                    original = r["path"]
                    r["path"] = mount_path.rstrip("/") + ("" if original == "/" else original)
                    if not r["path"]:
                        r["path"] = "/"
                    r["_from_mount"] = mount_path
                    # WS endpoints carry a synthetic route + endpoint_ctx
                    # that were built from the sub-app's internal path.
                    # Patch them with the full (mount-prefixed) path so
                    # ``ws.scope["route"].path`` and
                    # ``WebSocketRequestValidationError.endpoint_path``
                    # reflect the real URL the client hit.
                    if r.get("is_websocket"):
                        ep = r.get("endpoint")
                        if ep is not None:
                            try:
                                rt = getattr(ep, "_ws_synthetic_route", None)
                                if rt is not None:
                                    rt.path = r["path"]
                                ctx = getattr(ep, "_ws_endpoint_ctx", None)
                                if isinstance(ctx, dict):
                                    ctx["path"] = r["path"]
                            except Exception:  # noqa: BLE001
                                pass
                all_routes.extend(sub_routes)
                # Also add a passthrough route so GET <mount>/openapi.json
                # serves the sub-app's own schema (with `servers: [{"url":
                # <mount>}]` auto-prefixed via root_path).
                if mounted_app.openapi_url:
                    _sub_openapi_path = (
                        mount_path.rstrip("/") + mounted_app.openapi_url
                    )
                    # Force root_path so the sub-app's schema advertises its
                    # mount point, mirroring Starlette's mount behaviour.
                    if not mounted_app.root_path:
                        mounted_app.root_path = mount_path.rstrip("/")

                    def _make_openapi_handler(_app):
                        def _openapi_endpoint():
                            return _app.openapi()
                        _openapi_endpoint.__name__ = "openapi"
                        return _openapi_endpoint

                    all_routes.append({
                        "path": _sub_openapi_path,
                        "methods": ["GET"],
                        "endpoint": _make_openapi_handler(mounted_app),
                        "is_async": False,
                        "handler_name": f"openapi_{id(mounted_app)}",
                        "params": [],
                        "is_websocket": False,
                        "_from_mount": mount_path,
                        "include_in_schema": False,
                    })
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
        """Return the OpenAPI schema dict (cached after first call).

        FA convention: ``app.openapi_schema`` is a public, user-mutable
        cache. Users can either override ``app.openapi`` entirely (custom
        generator fn) or mutate the cached dict after first call.
        """
        if getattr(self, "openapi_schema", None) is not None:
            return self.openapi_schema
        route_dicts = self._collect_all_routes()
        # Exclude routes that come from mounted sub-FastAPI apps —
        # each mounted app owns its own schema at `<mount>/openapi.json`.
        route_dicts = [r for r in route_dicts if not r.get("_from_mount")]
        # Add root_path to servers if configured (matches run_server() behavior)
        effective_servers = self.servers
        if self.root_path and self.root_path_in_servers:
            if not effective_servers:
                effective_servers = [{"url": self.root_path}]
            elif not any(s.get("url") == self.root_path for s in effective_servers):
                effective_servers = [{"url": self.root_path}, *effective_servers]
        webhook_dicts = self._collect_routes_from_router(self.webhooks)
        self.openapi_schema = generate_openapi_schema(
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
        return self.openapi_schema

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    def _run_startup_handlers(self) -> None:
        """Execute all registered startup handlers on the shared worker loop
        so that connection pools / asyncio resources created during startup
        remain bound to a live event loop for the lifetime of the app
        (otherwise `asyncio.run(...)` would close the loop immediately,
        invalidating asyncpg pools / redis.asyncio clients etc.).
        """
        from fastapi_rs._async_worker import submit as _submit
        for handler in self._collect_startup_handlers():
            if inspect.iscoroutinefunction(handler):
                _submit(handler())
            else:
                handler()

    def _run_shutdown_handlers(self) -> None:
        """Execute all registered shutdown handlers on the worker loop."""
        from fastapi_rs._async_worker import submit as _submit
        for handler in self._collect_shutdown_handlers():
            if inspect.iscoroutinefunction(handler):
                _submit(handler())
            else:
                handler()

    def _collect_startup_handlers(self) -> list:
        """App-level startup handlers first, then every nested router's."""
        out = list(self._on_startup)

        def _walk(r):
            out.extend(getattr(r, "_on_startup", None) or [])
            for entry in getattr(r, "_included_routers", None) or []:
                child = entry[0]
                _walk(child)
        _walk(self.router)
        for entry in self._included_routers:
            _walk(entry[0])
        return out

    def _collect_shutdown_handlers(self) -> list:
        """App + nested-router shutdown handlers, in reverse-startup order."""
        handlers: list = []

        def _walk(r):
            for entry in getattr(r, "_included_routers", None) or []:
                child = entry[0]
                _walk(child)
            handlers.extend(getattr(r, "_on_shutdown", None) or [])
        for entry in self._included_routers:
            _walk(entry[0])
        _walk(self.router)
        handlers.extend(self._on_shutdown)
        return handlers

    def _collect_lifespans(self) -> list:
        """Return app + nested-router lifespans in depth-first order.

        Order matters: child lifespans start first (entered before the
        parent's yielded state is merged in) so the parent's yielded
        keys win on collision. Shutdown runs in reverse: parent's exit
        runs first, then children unwind.
        """
        out: list = []

        def _walk(r):
            lf = getattr(r, "lifespan", None)
            if lf is not None:
                out.append(lf)
            for entry in getattr(r, "_included_routers", None) or []:
                _walk(entry[0])

        # router's own routes too
        inner = getattr(self.router, "_included_routers", None) or []
        for entry in inner:
            _walk(entry[0])
        for entry in self._included_routers:
            _walk(entry[0])

        # App's lifespan LAST so it merges on top (parent wins on key collision).
        if self.lifespan is not None:
            out.append(self.lifespan)
        return out

    def _run_lifespan_startup(self) -> None:
        """Enter every lifespan (app + routers), merging yielded state
        into ``self._app_state`` and ``self.state``. Parent state
        overrides child on key collision.

        Idempotent: if ``_lifespan_cms`` is already populated (e.g.
        ``TestClient.__enter__`` ran startup before the server thread's
        ``app.run()`` also called this), skip — otherwise overwriting
        ``_lifespan_cms`` drops the prior generators, which close on
        GC and fire ``shutdown`` prematurely.
        """
        if getattr(self, "_lifespan_cms", None):
            return
        lifespans = self._collect_lifespans()
        if not lifespans:
            return

        from contextlib import asynccontextmanager as _acm
        from collections.abc import AsyncGenerator as _AsyncGen
        from collections.abc import Generator as _Gen
        import inspect as _inspect

        def _wrap(cb):
            """Coerce (a)sync-generator functions to async context managers."""
            # Already an @asynccontextmanager — calling it gives us an
            # async ctx manager. Detect by checking the return.
            def _probe():
                return cb(self)
            try:
                cm = _probe()
            except Exception:
                raise
            if hasattr(cm, "__aenter__"):
                return cm
            if _inspect.isasyncgen(cm):
                @_acm
                async def _agen_wrap(app):
                    it = cb(app)
                    val = await it.__anext__()
                    try:
                        yield val
                    finally:
                        try:
                            await it.__anext__()
                        except StopAsyncIteration:
                            pass
                return _agen_wrap(self)
            if _inspect.isgenerator(cm):
                @_acm
                async def _gen_wrap(app):
                    it = cb(app)
                    val = next(it)
                    try:
                        yield val
                    finally:
                        try:
                            next(it)
                        except StopIteration:
                            pass
                return _gen_wrap(self)
            # Plain callable returning a context manager
            if hasattr(cm, "__enter__"):
                @_acm
                async def _sync_cm_wrap():
                    val = cm.__enter__()
                    try:
                        yield val
                    finally:
                        cm.__exit__(None, None, None)
                return _sync_cm_wrap()
            return cm

        cms = [_wrap(lf) for lf in lifespans]
        self._lifespan_cms = cms
        merged: dict = {}

        async def _enter_all():
            for cm in cms:
                state = await cm.__aenter__()
                if state:
                    merged.update(state)
            self._app_state = merged
            for k, v in merged.items():
                setattr(self.state, k, v)

        from fastapi_rs._async_worker import submit as _submit
        _submit(_enter_all())

    def _run_lifespan_shutdown(self) -> None:
        """Exit every lifespan in reverse-start order."""
        cms = getattr(self, "_lifespan_cms", None)
        if not cms:
            return

        async def _exit_all():
            for cm in reversed(cms):
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:
                    pass

        from fastapi_rs._async_worker import submit as _submit
        _submit(_exit_all())
        self._lifespan_cms = None

    # ------------------------------------------------------------------
    # Server launch
    # ------------------------------------------------------------------

    def run(self, host: str = "127.0.0.1", port: int = 8000, **kwargs: Any) -> None:
        """Collect routes, hand them to the Rust core, and start serving."""
        from fastapi_rs._fastapi_rs_core import ParamInfo, RouteInfo, run_server

        # Run lifespan startup phase if lifespan is set (P0 fix #3).
        # Also trigger for router-level lifespans (test_router_events).
        if self._collect_lifespans():
            self._run_lifespan_startup()
            atexit.register(self._run_lifespan_shutdown)

        # Run startup event handlers (P0 fix #2)
        self._run_startup_handlers()

        # Register shutdown handlers via atexit (P0 fix #2). Check the
        # FULL chain (app + routers) — a router-only shutdown (no
        # app-level handler) must still run.
        if self._collect_shutdown_handlers():
            atexit.register(self._run_shutdown_handlers)

        # Register ``/openapi.json`` as a Python handler BEFORE route
        # collection so ``run_server`` hands it to Rust. The handler
        # regenerates the schema per-request, so changes to
        # ``app.root_path`` / ``app.servers`` between TestClient
        # instances surface immediately
        # (``test_openapi_cache_root_path``).
        _openapi_url_val = self.openapi_url
        if _openapi_url_val is not None:
            _app_ref = self

            def _openapi_dynamic():
                _app_ref.openapi_schema = None
                from fastapi_rs.responses import JSONResponse as _JR
                try:
                    _schema = _app_ref.openapi()
                except Exception as _exc:  # noqa: BLE001
                    # Mirror FA: the openapi builder raises ValueError for
                    # invalid configs (e.g. non-numeric response status
                    # keys). TestClient asserts on ``pytest.raises(
                    # ValueError)`` — capture so it surfaces at the caller.
                    _app_ref._captured_server_exceptions.append(_exc)
                    raise
                return _JR(content=_schema)

            _openapi_dynamic.__name__ = "openapi"
            # Drop any existing dynamic route from a prior ``app.run()``
            # (some test suites re-run the same app multiple times).
            def _is_prior_dynamic(r):
                ep = getattr(r, "endpoint", None)
                return (
                    ep is not None
                    and getattr(ep, "__name__", None) == "openapi"
                    and getattr(r, "path", None) == _openapi_url_val
                )
            self.router.routes = [
                r for r in self.router.routes if not _is_prior_dynamic(r)
            ]
            _openapi_route = APIRoute(
                _openapi_url_val,
                _openapi_dynamic,
                methods=["GET"],
                include_in_schema=False,
            )
            # Bypass app/router dependencies — docs shouldn't require
            # user-level auth headers.
            _openapi_route._fastapi_rs_bypass_deps = True
            self.router.routes.insert(0, _openapi_route)

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

        # Generate the OpenAPI schema JSON if docs are enabled. Honour a
        # user-supplied ``app.openapi = my_function`` override (FA's
        # extending_openapi tutorial).
        openapi_json: str | None = None
        if self.openapi_url is not None:
            try:
                openapi_schema = self.openapi()
            except Exception:  # noqa: BLE001
                openapi_schema = None
            if openapi_schema is not None:
                # Use ``JSONEncoder().encode`` instead of
                # ``json.dumps`` so tests that monkey-patch
                # ``json.dumps`` (``test_dump_json_fast_path``) don't
                # flag our internal openapi serialization.
                openapi_json = json.JSONEncoder().encode(openapi_schema)

        # Dynamic openapi handler already registered above; null out
        # baked JSON so Rust's auto-registered ``/openapi.json`` route
        # is skipped. Keep ``openapi_url`` set because swagger/redoc
        # HTML uses it in the ``fetch('<url>')`` call.
        if _openapi_url_val is not None:
            openapi_json = None
        _openapi_url_for_rust = self.openapi_url

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
                # FA parity: populate ``RequestValidationError.body``
                # when Rust plumbs the raw JSON body alongside the
                # validation errors. ``test_handling_errors/test_tutorial005``
                # asserts ``exc.body`` equals the original request body.
                _body_for_rve = detail_obj.get("body") if isinstance(detail_obj, dict) else None
                exc = _RVE(errors_list, body=_body_for_rve)
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

        # Render Swagger UI / ReDoc HTML in Python so FA kwargs
        # (``swagger_ui_parameters``, ``swagger_ui_init_oauth``) are
        # honoured. Rust serves the rendered string verbatim.
        swagger_ui_html_str: str | None = None
        redoc_html_str: str | None = None
        if self.docs_url is not None and self.openapi_url is not None:
            try:
                import fastapi_rs.compat as _c
                _c.install()
                import sys
                _docs_mod = sys.modules.get("fastapi.openapi.docs")
                if _docs_mod is not None:
                    resp = _docs_mod.get_swagger_ui_html(
                        openapi_url=self.openapi_url,
                        title=self.title + " - Swagger UI",
                        oauth2_redirect_url=self.swagger_ui_oauth2_redirect_url,
                        init_oauth=self.swagger_ui_init_oauth,
                        swagger_ui_parameters=self.swagger_ui_parameters,
                    )
                    swagger_ui_html_str = resp.body.decode("utf-8") if hasattr(resp, "body") else None
            except Exception:  # noqa: BLE001
                swagger_ui_html_str = None
        if self.redoc_url is not None and self.openapi_url is not None:
            try:
                import fastapi_rs.compat as _c
                _c.install()
                import sys
                _docs_mod = sys.modules.get("fastapi.openapi.docs")
                if _docs_mod is not None:
                    resp = _docs_mod.get_redoc_html(
                        openapi_url=self.openapi_url,
                        title=self.title + " - ReDoc",
                    )
                    redoc_html_str = resp.body.decode("utf-8") if hasattr(resp, "body") else None
            except Exception:  # noqa: BLE001
                redoc_html_str = None

        run_server(
            route_infos,
            host,
            port,
            middleware_config,
            openapi_json,
            self.docs_url,
            self.redoc_url,
            _openapi_url_for_rust,
            static_mounts,
            self.root_path or None,
            self.redirect_slashes,
            self.max_request_size,
            not_found_handler,
            self,
            validation_handler,
            self.swagger_ui_oauth2_redirect_url,
            swagger_ui_html_str,
            redoc_html_str,
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
                    if self._collect_lifespans():
                        self._run_lifespan_startup()
                    self._run_startup_handlers()
                    await send({"type": "lifespan.startup.complete"})
                except Exception as exc:
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
                    return
            elif message["type"] == "lifespan.shutdown":
                try:
                    if getattr(self, "_lifespan_cms", None):
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
