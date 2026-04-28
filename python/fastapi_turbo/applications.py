"""The main FastAPI-compatible application class."""

from __future__ import annotations

import asyncio
import atexit
import inspect
import json
import logging
import os
from typing import Any, Callable, Sequence

# Module logger for the silently-swallowed paths. ``except Exception:
# pass`` used to be the default; where the swallow is genuinely
# defensive (optional integrations, best-effort introspection) we now
# emit a DEBUG record so a developer can opt in to tracing via
# ``logging.getLogger("fastapi_turbo.applications").setLevel(logging.DEBUG)``
# without adding runtime cost when the logger is at its default level.
_log = logging.getLogger("fastapi_turbo.applications")

# Sentry compat-shim helpers live in their own module so the Sentry-
# specific code path doesn't clutter the core dispatch logic here.
# See ``fastapi_turbo/_sentry_compat.py`` for the full set.
from fastapi_turbo._sentry_compat import (  # noqa: F401 — re-exported below
    _current_request_scope,
    _ensure_sentry_middleware,
    _maybe_install_sentry_request_event_processor,
    _maybe_sentry_capture_failed_request,
    _refine_request_scope_for_route,
    _refine_sentry_transaction,
    _refine_sentry_transaction_as_middleware,
    _RouteScope,
    _set_current_request_scope,
)



from fastapi_turbo._introspect import introspect_endpoint
from fastapi_turbo._openapi import generate_openapi_schema
from fastapi_turbo._resolution import build_resolution_plan, _make_sync_wrapper
from fastapi_turbo.datastructures import State
from fastapi_turbo.routing import APIRouter, APIRoute


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


# Route-handler helpers extracted to ``_route_helpers.py``.
from fastapi_turbo._route_helpers import (  # noqa: F401 — re-exports
    _apply_response_model,
    _apply_status_code,
    _build_custom_route_handler_endpoint,
    _build_default_route_handler,
    _close_one_upload,
    _close_upload_files,
    _has_overridden_get_route_handler,
    _is_async_callable,
    _maybe_print_debug_traceback,
    _model_needs_full_dump,
    _wrap_response_class,
)


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
                    except Exception as _exc:  # noqa: BLE001
                        _log.debug("silent catch in applications: %r", _exc)

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
                            from fastapi_turbo.exceptions import (
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
                handler_func = _make_sync_wrapper(handler_func, for_handler=True, app=app)

            _app_ref = app
            _route_path_for_scope = path
            _endpoint_for_scope = endpoint

            def _compiled_no_deps(**kwargs):
                # Stamp endpoint/route onto the request scope so Sentry's
                # ``_set_transaction_name_and_source`` switches from
                # URL-based naming (``http://host/path``) to route-based
                # (``/items/{item_id}``) or endpoint-based (``pkg.mod.fn``).
                _refine_request_scope_for_route(_endpoint_for_scope, _route_path_for_scope)
                # Drain any deferred extraction errors from Rust and
                # surface them as a single 422 — matches FA's "one JSON
                # body listing EVERY missing required field" behaviour
                # even when the handler itself has no dep chain.
                _pending = kwargs.pop("__fastapi_turbo_extraction_errors__", None)
                _raw_body_str = kwargs.pop("__fastapi_turbo_raw_body_str__", None)
                kwargs.pop("__fastapi_turbo_raw_body_bytes__", None)
                if _pending is not None:
                    from fastapi_turbo.responses import JSONResponse as _JSONResp
                    import json as _json
                    detail = _json.loads(_pending)
                    # FA parity: ``RequestValidationError.body`` holds
                    # the raw JSON body (dict) so custom exception
                    # handlers can inspect what the caller sent.
                    _rve_body = None
                    if _raw_body_str is not None:
                        try:
                            _rve_body = _json.loads(_raw_body_str)
                        except Exception as _exc:  # noqa: BLE001
                            _log.debug("silent catch in applications: %r", _exc)
                            _rve_body = _raw_body_str
                    try:
                        from fastapi_turbo.exceptions import (
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
                            except Exception as _exc:  # noqa: BLE001
                                _log.debug("silent catch in applications: %r", _exc)
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
                        from fastapi_turbo.exceptions import (
                            RequestValidationError as _RVE2,
                        )
                        if isinstance(_ccexc, _RVE2):
                            from fastapi_turbo.responses import (
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
                    # Sentry parity: HTTPException status codes in the
                    # integration's ``failed_request_status_codes`` set
                    # should emit events. Covers the no-custom-handler
                    # path where ``_invoke_exception_handler`` isn't
                    # reached.
                    _maybe_sentry_capture_failed_request(exc)
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
                        from fastapi_turbo.exceptions import HTTPException as _HE
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
                        from fastapi_turbo.exceptions import (
                            ResponseValidationError as _RVE2,
                        )
                        if (
                            _app_ref is not None
                            and _RVE2 in _app_ref.exception_handlers
                        ):
                            handler_raised = False
                            try:
                                hdl_result = _app_ref._invoke_exception_handler_strict(_rve)
                            except Exception as _exc:  # noqa: BLE001
                                _log.debug("silent catch in applications: %r", _exc)
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

            _compiled_no_deps._fastapi_turbo_original_endpoint = endpoint  # type: ignore[attr-defined]
            _compiled_no_deps._fastapi_turbo_defers_extraction_errors = True  # type: ignore[attr-defined]
            # Expose the endpoint-context dict so the mount-prefix path
            # patcher (``_collect_all_routes``) can rewrite ``ctx["path"]``
            # to the user-visible mount-prefixed URL — otherwise a
            # ``RequestValidationError`` / ``ResponseValidationError``
            # raised from a mounted sub-app shows the sub-app-internal
            # path (``/items/``) instead of what the client actually hit
            # (``/sub/items/``).
            _compiled_no_deps._fastapi_turbo_endpoint_ctx = _endpoint_ctx  # type: ignore[attr-defined]
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
            func = _make_sync_wrapper(func, app=app)
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

    _orig_endpoint = endpoint  # async fn retained for the batched-submit path
    handler_func = endpoint
    if inspect.iscoroutinefunction(handler_func):
        handler_func = _make_sync_wrapper(handler_func, for_handler=True, app=app)

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
        from fastapi_turbo.dependencies import Depends as _Dep
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
        # name ``__fastapi_turbo_override_request__``.
        _req = resolved_env.get("__fastapi_turbo_override_request__")
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
                except Exception as _exc:  # noqa: BLE001
                    _log.debug("silent catch in applications: %r", _exc)
                try:
                    hp = _req.headers
                    if pname in hp:
                        dk[pname] = hp[pname]
                        continue
                except Exception as _exc:  # noqa: BLE001
                    _log.debug("silent catch in applications: %r", _exc)
                try:
                    cp = _req.cookies
                    if pname in cp:
                        dk[pname] = cp[pname]
                        continue
                except Exception as _exc:  # noqa: BLE001
                    _log.debug("silent catch in applications: %r", _exc)

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
                    sub_val = _make_sync_wrapper(effective, app=app)(**sub_dk)
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
                except Exception as _exc:  # noqa: BLE001
                    _log.debug("silent catch in applications: %r", _exc)
                if missing:
                    from fastapi_turbo.exceptions import HTTPException as _HE
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
        # Stamp endpoint/route onto the request scope so Sentry can
        # refine transaction names from URL to route / endpoint style.
        _refine_request_scope_for_route(endpoint, path)
        # FastAPI semantics: a ``Depends(...)`` that raises
        # ``HTTPException`` short-circuits BEFORE parameter validation
        # errors surface. Rust collects extraction errors and stashes
        # them in a private kwarg so we can try running deps first —
        # if any dep raises ``HTTPException`` that response wins; if
        # all deps succeed we then emit the queued 422.
        _raw_body_str_pending = kwargs.pop("__fastapi_turbo_raw_body_str__", None)
        kwargs.pop("__fastapi_turbo_raw_body_bytes__", None)
        _pending_extraction_errors_json = kwargs.pop(
            "__fastapi_turbo_extraction_errors__", None
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
                            actual_func = _make_sync_wrapper(actual_func, app=app)

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
                        from fastapi_turbo.security import SecurityScopes as _SS
                        dk[_ss_param] = _SS(scopes=list(_ss_list))
                    except Exception as _exc:  # noqa: BLE001
                        _log.debug("silent catch in applications: %r", _exc)

                if override_used is not None:
                    # Override may have a different signature from the
                    # original (fewer params, or its own Depends sub-deps).
                    # Drop kwargs the override doesn't accept, and resolve
                    # its sub-deps via a lazily-built mini-plan.
                    dk = _resolve_override_kwargs(
                        override_used, dk, resolved, _app, cache
                    )

                if is_generator:
                    # Generator dep (yield) support.
                    gen = actual_func(**dk)
                    if inspect.isasyncgen(gen):
                        # Dispatch strategy for async yield-deps:
                        #
                        # * ASYNC handler → run the whole dep lifecycle
                        #   (setup + teardown) on the worker loop. This
                        #   is the only path that is safe for async
                        #   resources whose teardown uses
                        #   ``asyncio.create_task`` /
                        #   ``get_running_loop`` (SQLAlchemy's
                        #   ``AsyncSession.__aexit__``, asyncpg pool
                        #   release, redis.asyncio). Driving those
                        #   teardowns via sync ``coro.send(None)`` on
                        #   the request thread raises
                        #   ``RuntimeError: no running event loop`` —
                        #   and trying to recover by calling
                        #   ``gen.__anext__()`` again corrupts the
                        #   generator ("cannot reuse already awaited
                        #   aclose()"). Always submit on the worker.
                        #
                        # * SYNC handler → keep the try-sync fast path
                        #   so that contextvar mutations inside the
                        #   async-gen are visible to the handler on the
                        #   same thread (FA parity via
                        #   ``test_dependency_contextvars``). No async
                        #   resource teardown is expected here — if the
                        #   gen is ContextVar-only, it never touches the
                        #   loop.
                        _handler_is_async = inspect.iscoroutinefunction(endpoint)
                        if _handler_is_async:
                            from fastapi_turbo._async_worker import submit as _submit
                            result = _submit(gen.__anext__(), app=_app)
                            generators_to_cleanup.append((gen, "worker", dep_scope))
                        else:
                            _anext_coro = gen.__anext__()
                            _sync_ok = False
                            try:
                                _anext_coro.send(None)
                            except StopIteration as _stop:
                                result = _stop.value
                                _sync_ok = True
                            except BaseException:
                                _anext_coro.close()
                                raise
                            if _sync_ok:
                                generators_to_cleanup.append((gen, None, dep_scope))
                            else:
                                # Suspended on a real await — continue
                                # the partial coro on the worker loop.
                                # (Trace-fidelity tests with 5-middleware
                                # + async yield-deps rely on this not
                                # restarting a fresh __anext__.)
                                import asyncio as _asyncio
                                from fastapi_turbo._async_worker import get_loop as _get_loop
                                _loop = _get_loop()
                                _fut = _asyncio.run_coroutine_threadsafe(_anext_coro, _loop)
                                result = _fut.result(timeout=30)
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
                from fastapi_turbo.exceptions import HTTPException as _HE
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
            from fastapi_turbo.responses import JSONResponse as _JSONResp
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
                    from fastapi_turbo.exceptions import (
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
                        except Exception as _exc:  # noqa: BLE001
                            _log.debug("silent catch in applications: %r", _exc)
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
                    from fastapi_turbo.exceptions import (
                        RequestValidationError as _RVE3,
                    )
                    if isinstance(_ccexc2, _RVE3):
                        from fastapi_turbo.responses import (
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
                            reversed(generators_to_cleanup), throw_exc=exc, app=_app
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
                    from fastapi_turbo.exceptions import HTTPException as _HE
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
                    from fastapi_turbo.exceptions import (
                        ResponseValidationError as _RVE2,
                    )
                    if (
                        _app is not None
                        and _RVE2 in _app.exception_handlers
                    ):
                        handler_raised = False
                        try:
                            hdl_result = _app._invoke_exception_handler_strict(_rve)
                        except Exception as _exc:  # noqa: BLE001
                            _log.debug("silent catch in applications: %r", _exc)
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
            except Exception as _exc:  # noqa: BLE001
                _log.debug("silent catch in applications: %r", _exc)
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
                    app=_app,
                )
            if _defer_teardown and _mw_req is not None:
                _mw_req._pending_teardowns.extend(
                    reversed(_request_scope_tds)
                )
            elif _request_scope_tds:
                # FA 0.120+ ``scope="request"`` for a StreamingResponse must
                # defer teardown until AFTER the body iterator is fully
                # consumed (the body peeks at state mutated by the dep).
                from fastapi_turbo.responses import StreamingResponse as _SR2
                _final_result = _final_result_holder[0]
                if isinstance(_final_result, _SR2):
                    _orig_iter = _final_result.body_iterator
                    _tds = list(reversed(_request_scope_tds))
                    import inspect as _insp
                    if _insp.isasyncgen(_orig_iter) or hasattr(_orig_iter, "__anext__"):
                        async def _wrap_iter(orig=_orig_iter, tds=_tds, app_ref=_app):
                            try:
                                async for item in orig:
                                    yield item
                            finally:
                                _run_pending_teardowns(tds, app=app_ref)
                        _final_result.body_iterator = _wrap_iter()
                    else:
                        def _wrap_iter_sync(orig=_orig_iter, tds=_tds, app_ref=_app):
                            try:
                                for item in orig:
                                    yield item
                            finally:
                                _run_pending_teardowns(tds, app=app_ref)
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
                        app=_app,
                    )
                    if _td_errs and _app is not None:
                        for _td_exc in _td_errs:
                            _app._captured_server_exceptions.append(_td_exc)

    # Marker for the Rust router: this compiled handler knows how to
    # consume a deferred extraction-errors blob, so Rust should stash
    # (not raise) 422s and let dep bodies run first — matching FA's
    # "HTTPException from Depends wins over param validation" rule.
    _compiled._fastapi_turbo_original_endpoint = endpoint  # type: ignore[attr-defined]
    _compiled._fastapi_turbo_defers_extraction_errors = True  # type: ignore[attr-defined]
    # Mount-prefix patching hook — see ``_compiled_no_deps``.
    _compiled._fastapi_turbo_endpoint_ctx = _endpoint_ctx  # type: ignore[attr-defined]

    # ── Batched-submit fast path ──────────────────────────────────────────
    # When the handler is async AND any dep in the chain is an async-gen
    # (SQLAlchemy AsyncSession, asyncpg/redis.asyncio pools) the generic
    # ``_compiled`` above hops the worker loop THREE times per request —
    # dep setup, handler await, dep teardown. Each hop is a
    # ``run_coroutine_threadsafe`` round-trip (~100 μs). uvicorn runs all
    # three on one loop; that was our 25% deficit on the SQLA async row.
    #
    # Collapse the three hops into one by executing the async-dep setup +
    # handler await + async-dep teardown INSIDE a single coroutine and
    # submitting that once. Only activate when the chain is "easy": no
    # security-scopes plumbing, no sync generators mixed in. If anything
    # at call time smells complex (dep_overrides, SecurityScopes, etc.) we
    # fall back to the generic ``_compiled`` closure.
    _handler_is_coro = inspect.iscoroutinefunction(_orig_endpoint)
    _chain_all_simple = all(
        # async gen yield dep OK
        (is_gen and inspect.isasyncgenfunction(func))
        # plain async fn OK
        or (not is_gen and inspect.iscoroutinefunction(
            getattr(func, "_fastapi_turbo_original_async", func)
        ))
        # plain sync callable OK (after generator check)
        or (not is_gen and not inspect.isasyncgenfunction(func))
        for _name, func, _orig, _imap, _fid, is_gen, _uc, _ssi, _sc in dep_chain
    )
    _has_async_yield_dep = any(
        is_gen and inspect.isasyncgenfunction(func)
        for _name, func, _orig, _imap, _fid, is_gen, _uc, _ssi, _sc in dep_chain
    )
    _any_sec_scope = any(ssi is not None for *_x, ssi, _sc in dep_chain)
    _any_sync_gen = any(
        is_gen and not inspect.isasyncgenfunction(func)
        for _name, func, _orig, _imap, _fid, is_gen, _uc, _ssi, _sc in dep_chain
    )

    if (
        _handler_is_coro
        and _has_async_yield_dep
        and not _any_sec_scope
        and not _any_sync_gen
    ):
        from fastapi_turbo._async_worker import submit as _worker_submit

        # Precompute which deps are async (yield vs fn) vs plain sync.
        # Tuple: (name, func, orig_func, input_map, func_id, kind, use_cache)
        # kind ∈ {"async_gen", "async_fn", "sync_fn"}
        _fp_chain: list[tuple] = []
        for name, func, orig_func, input_map, func_id, is_gen, use_cache, _ssi, _sc in dep_chain:
            orig_async = getattr(func, "_fastapi_turbo_original_async", None)
            if is_gen:
                kind = "async_gen"
                call_fn = func  # async-gen fn
            elif orig_async is not None:
                kind = "async_fn"
                call_fn = orig_async  # unwrapped async fn
            elif inspect.iscoroutinefunction(func):
                kind = "async_fn"
                call_fn = func
            else:
                kind = "sync_fn"
                call_fn = func
            _fp_chain.append(
                (name, call_fn, orig_func, input_map, func_id, kind, use_cache)
            )

        _fp_endpoint = _orig_endpoint  # the true async handler

        def _compiled_fast(**kwargs):
            # Stamp endpoint/route onto the request scope so Sentry can
            # refine transaction names from URL to route / endpoint style.
            _refine_request_scope_for_route(endpoint, path)
            # Runtime escape hatches — fall back to the generic path.
            if _app is not None and _app.dependency_overrides:
                return _compiled(**kwargs)

            kwargs.pop("__fastapi_turbo_raw_body_str__", None)
            kwargs.pop("__fastapi_turbo_raw_body_bytes__", None)
            _extract_err_json = kwargs.pop(
                "__fastapi_turbo_extraction_errors__", None
            )
            _mw_req = kwargs.get("_middleware_request")
            _defer_td = _mw_req is not None
            if _defer_td and not hasattr(_mw_req, "_pending_teardowns"):
                _mw_req._pending_teardowns = []

            resolved = kwargs
            _cache: dict = {}

            # PHASE 1 (submitted): resolve deps + run handler. Produces a
            # result snapshot AND the list of gens that need teardown.
            # Teardown MUST run after the response is captured (FA sends
            # the body / middleware reads state.copy() before teardown
            # mutates state), so it runs in a separate submit below.
            #
            # When the handler raises, yield-dep ``finally`` blocks must
            # still fire (in LIFO order) so DB connections close, state
            # dicts record the "finished" transition, etc. That teardown
            # has to run on the SAME loop as setup, so we perform it
            # inside this coro before re-raising.
            _async_gens_holder: list = []

            async def _setup_and_handler():
                async_gens = _async_gens_holder
                for name, call_fn, _orig, input_map, func_id, kind, use_cache in _fp_chain:
                    ck = (func_id, "request") if func_id is not None else None
                    if use_cache and ck is not None and ck in _cache:
                        resolved[name] = _cache[ck]
                        continue
                    dk = {
                        target: resolved[src]
                        for target, src in input_map
                        if src in resolved
                    }
                    if kind == "async_gen":
                        gen = call_fn(**dk)
                        val = await gen.__anext__()
                        async_gens.append(gen)
                    elif kind == "async_fn":
                        val = await call_fn(**dk)
                    else:
                        val = call_fn(**dk)
                    resolved[name] = val
                    if use_cache and ck is not None:
                        _cache[ck] = val

                if _extract_err_json:
                    from fastapi_turbo.responses import JSONResponse as _JR
                    import json as _json
                    return (_JR(
                        content={"detail": _json.loads(_extract_err_json)},
                        status_code=422,
                    ), async_gens)

                _hkwargs = {
                    k: resolved[k]
                    for k in handler_param_names
                    if k in resolved
                }
                for _ek, _ecls in _enum_coerce_deps.items():
                    if _ek in _hkwargs and isinstance(_hkwargs[_ek], str):
                        try:
                            _hkwargs[_ek] = _ecls(_hkwargs[_ek])
                        except (ValueError, KeyError):
                            pass
                try:
                    _apply_container_coerce(_hkwargs)
                except Exception as _ccexc:
                    from fastapi_turbo.exceptions import (
                        RequestValidationError as _RVEfp,
                    )
                    if isinstance(_ccexc, _RVEfp):
                        from fastapi_turbo.responses import JSONResponse as _JRfp
                        return (_JRfp(
                            content={"detail": list(_ccexc.errors())},
                            status_code=422,
                        ), async_gens)
                    raise

                try:
                    result = await _fp_endpoint(**_hkwargs)
                except BaseException as _h_exc:
                    # Handler raised. Drive each yield-dep's finally /
                    # except via ``athrow`` in reverse order so the dep
                    # can observe the exception and clean up on the
                    # same loop. A dep re-raising ``_h_exc`` is normal
                    # (the ``finally`` re-raises on exit); swallow that
                    # and keep unwinding. Any other exception from a
                    # dep's teardown also shouldn't mask the original.
                    _gens = list(_async_gens_holder)
                    _async_gens_holder.clear()
                    for _gen in reversed(_gens):
                        try:
                            await _gen.athrow(_h_exc)
                        except StopAsyncIteration:
                            pass
                        except BaseException as _td_exc:
                            if _td_exc is _h_exc:
                                pass  # finally re-raised the same exception
                            # else: swallow — original exception wins
                    raise
                # Snapshot via response_model BEFORE returning — this
                # decouples the result from the dep's shared-state dict
                # so post-submit teardown can't mutate the body.
                if response_model is not None and not hasattr(result, "status_code"):
                    result = _apply_response_model(
                        result,
                        response_model,
                        include=response_model_include,
                        exclude=response_model_exclude,
                        exclude_unset=response_model_exclude_unset,
                        exclude_defaults=response_model_exclude_defaults,
                        exclude_none=response_model_exclude_none,
                        by_alias=response_model_by_alias,
                        endpoint_ctx=_endpoint_ctx,
                    )
                return (result, async_gens)

            async def _run_teardown(gens):
                for gen in reversed(gens):
                    try:
                        await gen.__anext__()
                    except StopAsyncIteration:
                        pass

            try:
                # NOTE: do NOT catch & fall back here. The handler has side
                # effects (DB INSERTs, sequence bumps, external calls); a
                # fallback would re-run them, causing duplicated writes.
                # Let HTTPException / RequestValidationError / user errors
                # propagate — the outer handler turns them into responses.
                try:
                    result, _async_gens = _worker_submit(_setup_and_handler(), app=_app)
                except BaseException as _exc_fast:
                    # Capture non-HTTP handler errors so
                    # ``TestClient(raise_server_exceptions=True)`` can
                    # re-raise them in the test thread, matching
                    # Starlette's ASGI TestClient semantics.
                    try:
                        from fastapi_turbo.exceptions import HTTPException as _HEfp
                    except ImportError:
                        _HEfp = ()  # type: ignore[assignment]
                    if (
                        _app is not None
                        and not isinstance(_exc_fast, _HEfp)
                    ):
                        _app._captured_server_exceptions.append(_exc_fast)
                    raise

                # Post-process (response_class wrap / status_code) on main
                # thread so it doesn't block the worker loop.
                if (
                    response_class is not None
                    and not hasattr(result, "status_code")
                ):
                    try:
                        _rc = response_class
                        from fastapi_turbo.datastructures import DefaultPlaceholder as _DP
                        if isinstance(_rc, _DP):
                            _rc = getattr(_rc, "value", None)
                        if _rc is not None:
                            kwargs_rc = {}
                            if status_code is not None:
                                kwargs_rc["status_code"] = status_code
                            result = _rc(result, **kwargs_rc)
                    except Exception:
                        pass
                elif status_code is not None and not hasattr(result, "status_code"):
                    from fastapi_turbo.responses import JSONResponse as _JRsc
                    result = _JRsc(content=result, status_code=status_code)

                # Teardown. If middleware is in the chain, defer — the
                # outer MW wrapper drains ``_pending_teardowns`` AFTER the
                # middleware unwinds (so MW can still see pre-teardown
                # state via state.copy()). Otherwise fire-and-forget on
                # the worker loop so the main thread returns immediately
                # to Rust for response serialization. Teardown races in
                # parallel with serialization + next request's setup,
                # saving the ~120 μs synchronous teardown tail from our
                # per-request p50.
                #
                # Safety: if teardown hasn't released the DB connection
                # by the time the pool is empty, asyncpg/sqlalchemy will
                # queue the next acquire — no corruption, only back-
                # pressure. SQLAlchemy sessions with expire_on_commit=
                # False (parity default) are safe to close in the
                # background because their result objects are detached.
                if _defer_td and _async_gens:
                    for gen in reversed(_async_gens):
                        _mw_req._pending_teardowns.append((gen, "worker", "request"))
                elif _async_gens:
                    import asyncio as _asyncio
                    from fastapi_turbo._async_worker import get_loop as _get_loop_fast
                    try:
                        _asyncio.run_coroutine_threadsafe(
                            _run_teardown(_async_gens), _get_loop_fast()
                        )  # intentionally no .result() — fire-and-forget
                    except Exception:
                        pass

                return result
            finally:
                pass

        _compiled_fast._fastapi_turbo_original_endpoint = endpoint  # type: ignore[attr-defined]
        _compiled_fast._fastapi_turbo_defers_extraction_errors = True  # type: ignore[attr-defined]
        _compiled_fast._fastapi_turbo_endpoint_ctx = _endpoint_ctx  # type: ignore[attr-defined]
        return _compiled_fast

    return _compiled


def _run_pending_teardowns(
    teardowns,
    throw_exc: BaseException | None = None,
    propagate_exceptions: bool = False,
    collected_errors: list | None = None,
    app=None,
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
                from fastapi_turbo._async_worker import submit as _submit
                try:
                    if throw_exc is not None and hasattr(gen, "athrow"):
                        _submit(gen.athrow(throw_exc), app=app)
                    else:
                        _submit(gen.__anext__(), app=app)
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
                # ``loop=None`` — either a plain sync generator, or an
                # async generator that we drove via ``.send(None)``
                # (contextvar-preserving fast path). Detect async-gen
                # and step it via ``__anext__().send(None)``; fall
                # back to async worker if the teardown step itself
                # wants to suspend.
                import inspect as _ins
                if _ins.isasyncgen(gen):
                    # Async-gen teardown: try the sync fast path
                    # (`_tcoro.send(None)`). If it either suspends on a
                    # real await OR raises ``RuntimeError: no running
                    # event loop`` (e.g. SQLAlchemy async's
                    # ``__aexit__`` uses ``asyncio.create_task`` /
                    # ``get_running_loop``), we can't reuse the
                    # partially-driven coroutine — invoking the same
                    # coro object via ``run_coroutine_threadsafe``
                    # errors with "cannot reuse already awaited
                    # aclose()/athrow()". Instead, start a FRESH
                    # advancing coroutine on the worker loop via
                    # ``submit(gen.__anext__())`` (or ``gen.athrow``).
                    # The async-gen's internal state is preserved
                    # across ``__anext__()`` calls, so this resumes
                    # cleanly from where the yield paused.
                    if throw_exc is not None:
                        _tcoro = gen.athrow(throw_exc)
                    else:
                        _tcoro = gen.__anext__()
                    _needs_worker = False
                    try:
                        _tcoro.send(None)
                    except StopIteration:
                        if throw_exc is not None:
                            swallowed_handler_exc = True
                    except StopAsyncIteration:
                        if throw_exc is not None:
                            swallowed_handler_exc = True
                    except RuntimeError as _rt_err:
                        if "no running event loop" in str(_rt_err):
                            _needs_worker = True
                        else:
                            _tcoro.close()
                            raise
                    except BaseException:
                        _tcoro.close()
                        raise
                    else:
                        # Suspended on a real await — finish on worker.
                        _needs_worker = True
                    if _needs_worker:
                        # Don't ``_tcoro.close()`` here — that throws
                        # GeneratorExit INTO the async-gen via its
                        # partially-driven __anext__ coro, which effectively
                        # ``aclose``s the gen. The subsequent ``_submit(
                        # gen.__anext__())`` would then raise "cannot reuse
                        # already awaited aclose()/athrow()". Leaving the
                        # orphan coro to GC is safe — it has no side-effects
                        # beyond re-entering the gen body, which we're about
                        # to do on the worker loop anyway.
                        from fastapi_turbo._async_worker import submit as _submit
                        try:
                            if throw_exc is not None:
                                _submit(gen.athrow(throw_exc), app=app)
                            else:
                                _submit(gen.__anext__(), app=app)
                            if throw_exc is not None:
                                swallowed_handler_exc = True
                        except StopAsyncIteration:
                            if throw_exc is not None:
                                swallowed_handler_exc = True
                elif throw_exc is not None:
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
            from fastapi_turbo.exceptions import FastAPIError as _FE
            raise _FE(
                "No response returned. Either the view returned nothing "
                "or it is raising an exception and a dependency with "
                "yield caught the exception."
            ) from throw_exc


# Imports hoisted to module-level for the hot path (used by wrapped endpoints)
from fastapi_turbo.requests import Request as _Request
from fastapi_turbo.responses import JSONResponse as _JSONResponse


async def _ws_entry_with_asgi_chain(app_self, ws, path_params, inner_ws_entry):
    """Dispatch a synthesised ``scope['type'] == 'websocket'`` through the
    app's raw ASGI middleware chain, then call ``inner_ws_entry(ws, **path_params)``.

    Gives Sentry / OTel / rate-limit middleware connection-level visibility
    and exception capture. Per-message (``websocket.send`` / ``websocket.receive``)
    observation isn't plumbed — most tracing middleware keys off scope,
    not individual frames.
    """
    import asyncio

    # Build the ASGI scope from the WebSocket object.
    url = getattr(ws, "url", None)
    path = url.path if url is not None else "/"
    query = (url.query or "") if url is not None else ""
    raw_headers = []
    try:
        for k, v in (ws.headers.raw or []):
            kk = k.encode("latin-1") if isinstance(k, str) else k
            vv = v.encode("latin-1") if isinstance(v, str) else v
            raw_headers.append((kk, vv))
    except Exception as _exc:  # noqa: BLE001
        _log.debug("silent catch in applications: %r", _exc)
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": query.encode("latin-1"),
        "headers": raw_headers,
        "client": getattr(ws, "client", None),
        "server": getattr(ws, "server", None),
        "subprotocols": getattr(ws, "_subprotocols", []) or [],
        "state": {},
        "app": app_self,
    }

    # Receive queue: start with websocket.connect so the MW sees the handshake.
    recv_q: asyncio.Queue = asyncio.Queue()
    await recv_q.put({"type": "websocket.connect"})

    async def _recv():
        return await recv_q.get()

    async def _send(_msg):
        # Phase 1: no-op observer. MW still sees the scope and can catch
        # exceptions from ``await self.app(scope, receive, send)``.
        return None

    inner_exc: list = []

    async def _inner(s, r, _s):
        # Pull the connect event so MW-side ``receive()`` wrappers that
        # only consume one message stay consistent.
        msg = await r()
        if msg.get("type") != "websocket.connect":
            return
        # Run the actual WS handler. If it raises, propagate so an outer
        # MW's ``try/except`` can observe (Sentry / OTel).
        try:
            await inner_ws_entry(ws, **path_params)
        except BaseException as e:  # noqa: BLE001
            inner_exc.append(e)
            raise

    # Compose raw ASGI MW chain around the inner app (outer-most first).
    composed = _inner
    for mw_cls, kwargs in reversed(app_self._raw_asgi_middlewares):
        try:
            composed = mw_cls(app=composed, **kwargs)
        except TypeError:
            composed = mw_cls(**kwargs)

    try:
        await composed(scope, _recv, _send)
    except BaseException:  # noqa: BLE001
        # If the MW didn't swallow the handler's exception, surface it the
        # same way the non-chained path would: raise in the worker loop so
        # ``_ws_server_exceptions`` / TestClient capture logic fires.
        if inner_exc:
            raise inner_exc[0]
        raise


# Middleware-wrap machinery extracted to ``_middleware_wrap.py``.
from fastapi_turbo._middleware_wrap import (  # noqa: F401 — public-shape re-exports
    _drive_async_fallback,
    _make_asgi_middleware_shim,
    _MiddlewareSuspendedError,
    _wrap_with_http_middlewares,
)
def _collect_dependencies_from_markers(dependencies):
    """Convert a list of Depends markers into introspection-ready param dicts."""
    from fastapi_turbo.dependencies import Depends as DependsClass

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


def _find_exception_handler(app, exc):
    """Locate a custom exception handler for ``exc`` walking
    ``app.exception_handlers`` by MRO. Returns ``(handler, matched_cls)``
    where ``matched_cls`` is the class the handler was registered for
    (so callers can distinguish a custom-subclass handler from a
    catch-all ``Exception`` handler — only the latter re-raises after
    running, mirroring Starlette's ServerErrorMiddleware vs
    ExceptionMiddleware split). Returns ``(None, None)`` when no
    user-registered handler matches."""
    handlers = getattr(app, "exception_handlers", {}) or {}
    for cls in type(exc).__mro__:
        h = handlers.get(cls)
        if h is not None:
            return h, cls
    return None, None


async def _asgi_emit_exception(app, scope, send, exc):
    """Turn an exception raised during in-process dispatch into an
    ASGI response by (a) consulting ``app.exception_handlers`` for a
    user-registered handler, (b) falling back to FA-compatible
    defaults for HTTPException / RequestValidationError / other.

    Re-raise policy mirrors Starlette's ``ServerErrorMiddleware`` /
    ``ExceptionMiddleware`` split:

      * HTTPException, RequestValidationError, WebSocketException
        (and their subclasses) — these ARE the intended response
        types FastAPI uses to encode 4xx / 5xx outcomes. The handler
        sends a response and we return; never re-raise.
      * Generic ``Exception`` — the user may have registered a
        catch-all ``@app.exception_handler(Exception)`` to render a
        custom 500. Upstream Starlette runs that handler AND THEN
        re-raises so ``httpx.ASGITransport(raise_app_exceptions=True)``
        / ``TestClient(raise_server_exceptions=True)`` propagate the
        original exception out of the test, instead of silently
        masking a real failure as a successful 500. We match that
        contract: re-raise after delivering the response.

    Earlier impl returned silently after handler dispatch, so
    catch-all-handler tests in sandbox / serverless / in-process
    runs saw a 500 response instead of the unhandled exception —
    upstream sees the exception. Probe-confirmed divergence."""
    from fastapi_turbo.requests import Request as _Req
    from fastapi_turbo.responses import JSONResponse as _JR
    from fastapi_turbo.exceptions import (
        HTTPException as _HE,
        RequestValidationError as _RVE,
    )

    handler, matched_cls = _find_exception_handler(app, exc)
    if handler is not None:
        request = _Req(dict(scope))
        try:
            import inspect as _insp
            if _insp.iscoroutinefunction(handler):
                resp = await handler(request, exc)
            else:
                resp = handler(request, exc)
                if _insp.iscoroutine(resp):
                    resp = await resp
            await _send_asgi_response(send, resp)
            # Re-raise rule mirrors Starlette: a handler registered
            # for ``Exception`` itself (catch-all) sits in
            # ``ServerErrorMiddleware``, which RE-RAISES after running
            # so ``raise_app_exceptions=True`` test transports
            # propagate the original exception. Handlers registered
            # for a more specific class (HTTPException,
            # CustomError, etc.) live in ``ExceptionMiddleware``,
            # which sends the response and stops there. Distinguish
            # by ``matched_cls is Exception``.
            if matched_cls is Exception:
                raise exc
            return
        except Exception as handler_exc:  # noqa: BLE001
            if handler_exc is exc:
                # The deliberate re-raise above; let it propagate.
                raise
            # Handler itself blew up — fall through to default
            # rendering of the original exception.
            pass

    if isinstance(exc, _HE):
        headers = getattr(exc, "headers", None) or {}
        # Per RFC 9110 + Starlette: HTTP 1xx / 204 / 304 MUST NOT
        # carry a body. Earlier we always emitted ``{"detail":null}``
        # which broke ``test_starlette_exception::test_no_body_status_
        # code_exception_handlers`` (response.content asserted empty).
        from fastapi_turbo.responses import Response as _PlainResp
        if exc.status_code in (204, 304) or 100 <= exc.status_code < 200:
            resp = _PlainResp(status_code=exc.status_code)
        else:
            resp = _JR(content={"detail": exc.detail}, status_code=exc.status_code)
        for k, v in headers.items():
            resp.headers[k] = v
        await _send_asgi_response(send, resp)
        return
    if isinstance(exc, _RVE):
        # Pydantic ``ctx`` may carry ``Decimal`` / ``Path`` /
        # other JSON-non-native types that explode in
        # ``json.dumps``. Run the error list through ``jsonable_
        # encoder`` first so Decimal → float, datetime → str, etc.
        # Probe-confirmed against
        # ``test_multi_body_errors::test_jsonable_encoder_requiring_error``.
        from fastapi_turbo.encoders import jsonable_encoder as _je2
        await _send_asgi_response(
            send, _JR(
                content={"detail": _je2(exc.errors())}, status_code=422
            )
        )
        return
    # Unhandled non-FA exception. Upstream Starlette's
    # ``ServerErrorMiddleware`` ALWAYS sends a 500 with body
    # ``Internal Server Error`` first, then re-raises so
    # ``raise_server_exceptions=True`` tests still see the
    # exception. With ``raise_server_exceptions=False``, the
    # transport catches the re-raise and the rendered 500 body
    # is what the test sees. Probe-confirmed against
    # ``test_dependency_after_yield_streaming::test_broken_session_
    # data_no_raise``: expects ``response.text == "Internal Server
    # Error"`` under ``raise_server_exceptions=False``.
    try:
        from fastapi_turbo.responses import PlainTextResponse as _PTR
        await _send_asgi_response(
            send,
            _PTR("Internal Server Error", status_code=500),
        )
    except Exception:  # noqa: BLE001
        pass
    raise exc


def _parse_range_header(header_val: str, total_len: int):
    """Parse an RFC 7233 ``Range:`` header against a known file length.

    Implements Starlette 1.0's exact semantics:

      * Unit must be ``bytes`` (case-insensitive token — ``Bytes=`` is
        accepted, ``items=`` is rejected).
      * Per sub-range, parse ``start-end`` as Starlette does
        (internally end-exclusive: ``start = file_size - n`` for the
        ``-n`` suffix form; ``end = end_str + 1`` if both halves are
        present and ``end_str < file_size``, else ``end = file_size``).
      * Sub-ranges that fail to parse (``abc-def``, empty, no dash) are
        silently dropped (matches Starlette's ``_parse_ranges``).
      * Validation order: zero parseable → 400; any out-of-bounds
        start → 416; any reversed ``start > end`` → 400.
      * Overlapping/adjacent sub-ranges are merged before deciding
        single vs multipart (so ``0-19,0-19`` → single, ``0-9,10-19``
        → single).

    Returns one of:
      * ``('full',)`` — header absent. Caller serves 200 full body.
      * ``('range', start, end_inclusive)`` — single satisfiable
        coalesced range.
      * ``('multi', [(s0, e0), ...])`` — multiple satisfiable
        coalesced ranges. Caller emits 206 multipart/byteranges.
      * ``('unsatisfiable',)`` — well-formed but at least one
        sub-range start is out of bounds. Caller returns 416.
      * ``('malformed', detail)`` — Starlette ``MalformedRangeHeader``
        equivalent. Caller returns 400.

    No range-count cap: post-coalesce the byte sum is bounded by
    ``total_len`` (coalesced ranges are non-overlapping within the
    file), so the only "amplification" is the multipart envelope
    overhead (~150 bytes per range). At 1000 ranges that's ~150 KiB
    of framing — not a DoS surface — and it matches upstream's lack
    of a cap.
    """
    if not header_val:
        return ("full",)
    v = header_val.strip()
    # Error message strings match Starlette 1.0 byte-for-byte so
    # error-body comparisons across the two stacks pass.
    if "=" not in v:
        return ("malformed", "Malformed range header.")
    unit, _, rest = v.partition("=")
    if unit.strip().lower() != "bytes":
        return ("malformed", "Only support bytes range")
    rest = rest.strip()
    if total_len == 0:
        # Any well-formed range against an empty resource is
        # unsatisfiable per RFC 7233.
        return ("unsatisfiable",)

    # Parse sub-ranges in Starlette's half-open ``[start, end)``
    # representation. Per-sub-range errors are silently dropped.
    raw: list[tuple[int, int]] = []
    for part in rest.split(","):
        part = part.strip()
        if not part or part == "-":
            continue
        if "-" not in part:
            continue
        start_str, _, end_str = part.partition("-")
        start_str = start_str.strip()
        end_str = end_str.strip()
        try:
            if start_str:
                start = int(start_str)
            else:
                # ``-N`` suffix: start = file_size - N. Note this can
                # go negative for ``-N`` where N > file_size, which
                # the bounds check below catches as 416.
                start = total_len - int(end_str)
            if start_str and end_str and int(end_str) < total_len:
                end = int(end_str) + 1
            else:
                end = total_len
        except (ValueError, TypeError):
            continue
        raw.append((start, end))

    if not raw:
        return ("malformed", "Range header: range must be requested")

    # Bounds check (fires BEFORE the reversed check — matches
    # Starlette's order). Any start outside ``[0, total_len)`` → 416.
    if any(not (0 <= s < total_len) for s, _ in raw):
        return ("unsatisfiable",)

    if any(s > e for s, e in raw):
        return ("malformed", "Range header: start must be less than end")

    # Coalesce in half-open form (touching: ``s <= prev_end``).
    raw.sort()
    coalesced: list[tuple[int, int]] = []
    for s, e in raw:
        if coalesced and s <= coalesced[-1][1]:
            ps, pe = coalesced[-1]
            coalesced[-1] = (ps, max(pe, e))
        else:
            coalesced.append((s, e))

    # Convert half-open back to inclusive for the wire / caller.
    inclusive = [(s, e - 1) for s, e in coalesced]
    if len(inclusive) == 1:
        s, e = inclusive[0]
        return ("range", s, e)
    return ("multi", inclusive)


def _make_byteranges_boundary() -> str:
    """Generate a multipart/byteranges boundary. 26 hex chars —
    process-nanos ⊕ per-call counter for uniqueness across bursts."""
    import secrets
    import time
    return f"{int(time.time() * 1e9):016x}{secrets.token_hex(5)}"


async def _send_file_response_asgi(send, response, scope=None) -> None:
    """Serialize a ``FileResponse`` over ASGI, honouring the request's
    ``Range:`` header when present.

    ``FileResponse`` stores ``content=b""`` and relies on the Rust
    server to read ``self.path`` from disk at serve-time. Over the
    in-process ASGI path that never runs, so we open the file here,
    compute the slice we actually need, and stream it via
    ``http.response.body`` frames."""
    import os
    from fastapi_turbo.responses import JSONResponse as _JR_file

    path = getattr(response, "path", None)
    if path is None:
        await _send_asgi_response(send, _JR_file(
            content={"detail": "FileResponse has no path"},
            status_code=500,
        ))
        return

    try:
        stat = os.stat(path)
    except FileNotFoundError:
        await _send_asgi_response(send, _JR_file(
            content={"detail": f"File not found: {path}"},
            status_code=404,
        ))
        return
    except OSError as e:
        await _send_asgi_response(send, _JR_file(
            content={"detail": f"File stat error: {e}"},
            status_code=500,
        ))
        return

    import stat as _stat_mod
    if not _stat_mod.S_ISREG(stat.st_mode):
        # A directory (or device / fifo) reached FileResponse — this
        # is a server-side routing bug, not a client error. Match
        # Starlette: raise RuntimeError so the traceback surfaces in
        # dev logs; the ASGI error handler (or the surrounding
        # exception middleware) converts it to 500 for the client.
        # Silently returning a JSON 500 here would mask the misuse.
        raise RuntimeError(f"File at path {path} is not a file.")

    total_len = stat.st_size

    # Stamp Last-Modified + ETag at serve-time (matches Starlette).
    # ``set_stat_headers`` uses ``setdefault`` so any user-supplied
    # overrides on the response survive.
    try:
        response.set_stat_headers(stat)
    except Exception:
        # Don't let a header-stamp failure break the response.
        pass

    # Extract Range + If-Range from scope headers (if provided).
    range_header = ""
    if_range_header = ""
    if scope is not None:
        for hk, hv in scope.get("headers", []) or []:
            hkn = hk.decode("latin-1") if isinstance(hk, bytes) else hk
            kl = hkn.lower()
            if kl == "range":
                range_header = hv.decode("latin-1") if isinstance(hv, bytes) else hv
            elif kl == "if-range":
                if_range_header = hv.decode("latin-1") if isinstance(hv, bytes) else hv

    # If-Range gating: ignore Range when the validator doesn't match.
    # Per RFC 7233 §3.2, If-Range carries either the entity's ETag or
    # its Last-Modified — if neither matches, the server must serve the
    # full representation (status 200) rather than a 206.
    if range_header and if_range_header:
        lm = response.headers.get("last-modified", "")
        et = response.headers.get("etag", "")
        if if_range_header != lm and if_range_header != et:
            range_header = ""

    parsed = _parse_range_header(range_header, total_len) if range_header else ("full",)

    # 400 short-circuit (Starlette ``MalformedRangeHeader``).
    if parsed[0] == "malformed":
        detail = parsed[1] if len(parsed) > 1 else "malformed Range header"
        body = detail.encode("latin-1")
        await send({
            "type": "http.response.start",
            "status": 400,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("latin-1")),
            ],
        })
        await send({"type": "http.response.body", "body": body})
        return

    # 416 short-circuit. Header shape matches Starlette 1.0 exactly:
    # only Content-Range, Content-Length: 0, and Content-Type:
    # text/plain; charset=utf-8. No accept-ranges, no last-modified,
    # no etag — Starlette treats 416 as a generic PlainTextResponse.
    if parsed[0] == "unsatisfiable":
        await send({
            "type": "http.response.start",
            "status": 416,
            "headers": [
                (b"content-range", f"bytes */{total_len}".encode("latin-1")),
                (b"content-length", b"0"),
                (b"content-type", b"text/plain; charset=utf-8"),
            ],
        })
        await send({"type": "http.response.body", "body": b""})
        return

    # Multi-range: emit 206 multipart/byteranges, streaming each part.
    # Wire format mirrors Starlette 1.0's ``generate_multipart`` exactly
    # (CRLF separators, no leading CRLF, ``\r\n`` between body and next
    # part, closing ``--{boundary}--`` with no trailing CRLF):
    #
    #   --{boundary}\r\n
    #   Content-Type: {part_ct}\r\n
    #   Content-Range: bytes {start}-{end}/{total}\r\n
    #   \r\n
    #   <body bytes>
    #   \r\n--{boundary}\r\n
    #   Content-Type: {part_ct}\r\n
    #   ...
    #   \r\n--{boundary}--
    if parsed[0] == "multi":
        ranges: list[tuple[int, int]] = parsed[1]
        # Per-part Content-Type echoes the response's full content-
        # type (the FileResponse __init__ already augments textual
        # types with ``; charset=utf-8`` — same as Starlette). Falling
        # back to ``media_type`` would drop the charset.
        part_ct = (
            response.headers.get("content-type")
            or getattr(response, "media_type", None)
            or "application/octet-stream"
        )
        boundary = _make_byteranges_boundary()

        # Precompute each part's preamble and the total body length.
        # First preamble has no leading separator (matches Starlette);
        # subsequent preambles are prefixed with ``\r\n`` to terminate
        # the prior body (the body bytes end raw).
        preambles: list[bytes] = []
        body_len = 0
        for idx, (start, end) in enumerate(ranges):
            sep = "" if idx == 0 else "\r\n"
            pre = (
                f"{sep}--{boundary}\r\n"
                f"Content-Type: {part_ct}\r\n"
                f"Content-Range: bytes {start}-{end}/{total_len}\r\n"
                f"\r\n"
            ).encode("latin-1")
            preambles.append(pre)
            body_len += len(pre) + (end - start + 1)
        closing = f"\r\n--{boundary}--".encode("latin-1")
        body_len += len(closing)

        # Override headers for multipart response. We drop the upstream
        # response's content-type/content-length — the part media_type
        # lives inside each preamble now.
        response.headers["content-length"] = str(body_len)
        response.headers["content-type"] = (
            f"multipart/byteranges; boundary={boundary}"
        )
        if "accept-ranges" not in response.headers:
            response.headers["accept-ranges"] = "bytes"
        # Drop any single-range content-range left by earlier logic.
        response.headers.pop("content-range", None)

        norm_headers = _pack_asgi_headers(response)
        await send({
            "type": "http.response.start",
            "status": 206,
            "headers": norm_headers,
        })

        CHUNK = 64 * 1024
        try:
            with open(path, "rb") as fh:
                for (start, end), pre in zip(ranges, preambles):
                    await send({
                        "type": "http.response.body",
                        "body": pre,
                        "more_body": True,
                    })
                    fh.seek(start)
                    remaining = end - start + 1
                    while remaining > 0:
                        chunk = fh.read(min(CHUNK, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        await send({
                            "type": "http.response.body",
                            "body": chunk,
                            "more_body": True,
                        })
        except OSError:
            pass
        await send({
            "type": "http.response.body",
            "body": closing,
            "more_body": False,
        })
        return

    # Compute slice window.
    if parsed[0] == "range":
        _, start_off, end_incl = parsed
        slice_len = end_incl - start_off + 1
        status_code = 206
        content_range = f"bytes {start_off}-{end_incl}/{total_len}"
    else:
        start_off = 0
        slice_len = total_len
        status_code = int(getattr(response, "status_code", 200) or 200)
        content_range = None

    # Stamp Content-Length from the slice we're about to send.
    response.headers["content-length"] = str(slice_len)
    if "content-type" not in response.headers:
        media = getattr(response, "media_type", None) or "application/octet-stream"
        response.headers["content-type"] = media
    if content_range is not None:
        response.headers["content-range"] = content_range
    if "accept-ranges" not in response.headers:
        response.headers["accept-ranges"] = "bytes"

    norm_headers = _pack_asgi_headers(response)

    await send({
        "type": "http.response.start",
        "status": status_code,
        "headers": norm_headers,
    })

    # Stream 64 KiB chunks, bounded to the requested slice.
    CHUNK = 64 * 1024
    remaining = slice_len
    try:
        with open(path, "rb") as fh:
            if start_off > 0:
                fh.seek(start_off)
            while remaining > 0:
                chunk = fh.read(min(CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                await send({
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": remaining > 0,
                })
    except OSError:
        # File disappeared mid-stream — client sees a truncated body.
        pass
    if remaining == slice_len:
        # We sent no body frames (zero-length file) — emit the
        # terminator.
        await send({"type": "http.response.body", "body": b"", "more_body": False})


def _pack_asgi_headers(response) -> list[tuple[bytes, bytes]]:
    """Serialize a Response's headers into ASGI's ``[(bytes, bytes)]``
    shape, honouring both the ``MutableHeaders`` view (the canonical
    single-value-per-name dict-ish) and any ``raw_headers`` (preserves
    duplicate-allowed names like ``Set-Cookie``). De-duplicates exact
    (name, value) collisions between the two sources."""
    hdrs = response.headers
    raw_headers = getattr(response, "raw_headers", None) or []
    norm_headers: list[tuple[bytes, bytes]] = []
    seen_exact: set[tuple[str, str]] = set()
    if hdrs is not None and hasattr(hdrs, "items"):
        for k, v in hdrs.items():
            kl = (k if isinstance(k, str) else k.decode("latin-1")).lower()
            vs = v if isinstance(v, str) else v.decode("latin-1")
            seen_exact.add((kl, vs))
            norm_headers.append((kl.encode("latin-1"), vs.encode("latin-1")))
    for k, v in raw_headers:
        kl = (k if isinstance(k, str) else k.decode("latin-1")).lower()
        vs = v if isinstance(v, str) else v.decode("latin-1")
        if (kl, vs) in seen_exact:
            continue
        seen_exact.add((kl, vs))
        norm_headers.append((kl.encode("latin-1"), vs.encode("latin-1")))
    return norm_headers


_REAL_STARLETTE_CLASS_CACHE: dict[tuple[str, str], object] = {}
_REAL_STARLETTE_LOAD_LOCK = __import__("threading").RLock()


def _load_real_starlette_class(submodule: str, classname: str):
    """Bypass the fastapi_turbo shim to load the REAL Starlette class.

    The shim hijacks ``sys.modules['starlette.*']`` so user code's
    ``from starlette.middleware.cors import CORSMiddleware`` resolves
    to our Tower-bound marker stub (which has no ``__call__``). For
    the in-process dispatcher we need the real Starlette
    implementation so CORS / GZip / HTTPSRedirect actually work for
    TestClient / ASGITransport users.

    Snapshots ``starlette.*`` from ``sys.modules``, evicts them,
    forces a fresh import (which finds the real installed package on
    disk), captures the class reference, then restores the shim
    modules so subsequent user-land imports still see our shim.
    Cached so the snapshot only happens once per (submodule, class).

    Thread-safety: the snapshot/restore window is guarded by a
    process-wide reentrant lock. ``add_middleware`` pre-loads each
    Tower-bound class at registration so the dispatcher's hot path
    is just a dict lookup — the slow path only runs during app
    construction (single-threaded by convention) or if a user
    side-channel adds middleware mid-request (uncommon)."""
    cache_key = (submodule, classname)
    cached = _REAL_STARLETTE_CLASS_CACHE.get(cache_key)
    if cached is not None:
        return cached

    import sys
    import importlib

    with _REAL_STARLETTE_LOAD_LOCK:
        # Re-check inside the lock to avoid duplicate loads when two
        # callers race past the unsynchronised lookup above.
        cached = _REAL_STARLETTE_CLASS_CACHE.get(cache_key)
        if cached is not None:
            return cached

        saved: dict[str, object] = {}
        for m in list(sys.modules):
            if m == "starlette" or m.startswith("starlette."):
                saved[m] = sys.modules[m]
                del sys.modules[m]

        importlib.invalidate_caches()
        try:
            mod = importlib.import_module(f"starlette.{submodule}")
            cls = getattr(mod, classname, None)
        except Exception:  # noqa: BLE001
            cls = None
        finally:
            # Drop anything imported during the un-shimmed window so the
            # shim remains canonical in sys.modules. THEN restore.
            for m in list(sys.modules):
                if (m == "starlette" or m.startswith("starlette.")) and m not in saved:
                    del sys.modules[m]
            for m, original in saved.items():
                sys.modules[m] = original

        _REAL_STARLETTE_CLASS_CACHE[cache_key] = cls
        return cls


_SENTRY_FASTAPI_HOOK_CACHE: dict = {"loaded": False, "fn": None, "integration_cls": None}


def _maybe_set_sentry_transaction_name(app, scope, matched_route) -> None:
    """If Sentry's ``FastApiIntegration`` is loaded, set the
    transaction name from ``scope['route'].path`` — replicating
    what Sentry's monkey-patched ``fastapi.routing.get_request_handler``
    would do on upstream FastAPI. Our dispatcher bypasses that
    handler, so the patch never fires; without this call, Sentry's
    legacy ``SentryAsgiMiddleware(app)`` setup falls back to the
    concrete-URL transaction name and ``test_legacy_setup`` diffs
    the URL against the expected route shape.

    No-op when Sentry isn't installed or the integration isn't
    loaded — the lookup is cached after the first call so the
    common no-Sentry path stays at one dict read per request."""
    cache = _SENTRY_FASTAPI_HOOK_CACHE
    if not cache["loaded"]:
        try:
            import sentry_sdk
            from sentry_sdk.integrations.fastapi import (
                FastApiIntegration,
                _set_transaction_name_and_source,
            )
            cache["sdk"] = sentry_sdk
            cache["fn"] = _set_transaction_name_and_source
            cache["integration_cls"] = FastApiIntegration
        except Exception:  # noqa: BLE001
            cache["fn"] = None
        cache["loaded"] = True

    fn = cache["fn"]
    if fn is None:
        return
    try:
        sentry_sdk = cache["sdk"]
        client = sentry_sdk.get_client()
        integration = client.get_integration(cache["integration_cls"])
        if integration is None:
            return
        # Sentry's helper expects a request-like with a ``.scope``
        # attribute; build a minimal shim around the live ASGI
        # scope so the function can read ``scope['route'].path``
        # / ``scope['endpoint']`` exactly as it would on upstream.
        class _RequestShim:
            __slots__ = ("scope",)

            def __init__(self, asgi_scope):
                self.scope = asgi_scope

        fn(
            sentry_sdk.get_current_scope(),
            integration.transaction_style,
            _RequestShim(scope),
        )
    except Exception:  # noqa: BLE001
        # Defensive: any failure inside Sentry's helper must not
        # affect the response. Sentry's own monkey-patches do the
        # same thing.
        pass


def _resolve_tower_bound_to_asgi_class(mw_cls):
    """Map a Tower-bound middleware marker class to its real
    Starlette ASGI3 equivalent so the in-process dispatcher can
    apply it like any other middleware. The Tower path uses these
    markers as routing flags only — they're inert as ASGI on their
    own. For the in-process / TestClient path we substitute the
    real Starlette class loaded around the shim.

    Accepts both class markers (with ``_fastapi_turbo_middleware_type``
    attribute) AND string-shorthand forms — ``app.add_middleware('cors',
    ...)`` registers the string directly so we look it up here too.

    Returns ``None`` if the class / string isn't a Tower-bound marker
    we know how to substitute."""
    if isinstance(mw_cls, str):
        mw_type = mw_cls
    else:
        mw_type = getattr(mw_cls, "_fastapi_turbo_middleware_type", None)
    if mw_type == "cors":
        return _load_real_starlette_class("middleware.cors", "CORSMiddleware")
    if mw_type == "gzip":
        return _load_real_starlette_class("middleware.gzip", "GZipMiddleware")
    if mw_type == "httpsredirect":
        return _load_real_starlette_class(
            "middleware.httpsredirect", "HTTPSRedirectMiddleware"
        )
    return None


def _resolve_response_class(matched_route, app):
    """Cascade: route.response_class →
    route._fastapi_turbo_effective_response_class (stamped at
    ``include_router`` time, carries the router-level or
    include-level default) → app.default_response_class →
    JSONResponse.

    Mirrors upstream's resolution order so handlers on a router with
    ``default_response_class=HTMLResponse`` (or included with
    ``include_router(..., default_response_class=…)``) correctly
    serialize string returns as HTML rather than JSON-quoting them."""
    from fastapi_turbo.responses import JSONResponse as _JR_default
    rc = getattr(matched_route, "response_class", None)
    if rc is not None:
        return rc
    rc = getattr(matched_route, "_fastapi_turbo_effective_response_class", None)
    if rc is not None:
        return rc
    rc = getattr(app, "default_response_class", None)
    if rc is not None:
        return rc
    return _JR_default


def _is_json_response_class(cls) -> bool:
    """True when ``cls`` is a JSON-style Response that wants its
    ``content`` to be a Python value (dict / list / Pydantic model
    etc.) rather than a pre-rendered string. Used by the in-process
    dispatch to decide whether to ``jsonable_encoder`` the raw
    handler return before passing it to the response constructor."""
    try:
        from fastapi_turbo.responses import (
            JSONResponse as _JR,
            ORJSONResponse as _OR,
            UJSONResponse as _UR,
        )
        return isinstance(cls, type) and issubclass(cls, (_JR, _OR, _UR))
    except ImportError:
        from fastapi_turbo.responses import JSONResponse as _JR
        return isinstance(cls, type) and issubclass(cls, _JR)


async def _send_asgi_response(send, response, scope=None) -> None:
    # ``FileResponse`` stores ``content=b""`` because the Rust server
    # reads ``response.path`` from disk at serve time. Over the in-
    # process ASGI path no Rust runs, so we must open + stream the
    # file ourselves — and honour the request's ``Range:`` header.
    try:
        from fastapi_turbo.responses import FileResponse as _FR
    except ImportError:
        _FR = None  # type: ignore[assignment]
    if _FR is not None and isinstance(response, _FR):
        # Bare propagation — if ``_send_file_response_asgi`` raises
        # (e.g. ``RuntimeError`` on directory paths, mirroring
        # Starlette), let the ASGI error handler surface it with a
        # traceback. The previous ``except Exception: pass`` swallowed
        # these and fell through to the generic path, which then
        # emitted a 200-empty body for a FileResponse that never
        # legitimately ran.
        await _send_file_response_asgi(send, response, scope=scope)
        return
    """Serialize a fastapi-turbo ``Response`` (or Starlette-compatible
    response with ``status_code`` / ``headers`` / ``body``) into ASGI
    ``http.response.start`` + ``http.response.body`` messages.

    Emits:
      * Every unique header via the dict-like ``.headers`` view
        (which includes mutations done via ``response.headers[k]=v``
        inside middleware).
      * Every duplicate (``Set-Cookie`` etc.) present in
        ``raw_headers`` / ``._extras`` that isn't already covered.

    Together this gives middleware a simple way to inject headers
    (``resp.headers['X-Traced'] = '1'``) AND preserves duplicate-header
    responses (two ``set_cookie`` calls → two ``Set-Cookie`` lines).
    """
    status_code = int(getattr(response, "status_code", 200) or 200)
    hdrs = getattr(response, "headers", None)
    raw_headers = getattr(response, "raw_headers", None) or []

    # De-dup via the dict view first; then append any raw entries
    # that differ from what the dict reports. ``_MutableHeadersDict``
    # guarantees key-canonical lowercase but value may have diverged
    # if the user called ``.append()`` for a second value.
    norm_headers: list[tuple[bytes, bytes]] = []
    seen_exact: set[tuple[str, str]] = set()
    if hdrs is not None and hasattr(hdrs, "items"):
        for k, v in hdrs.items():
            kl = (k if isinstance(k, str) else k.decode("latin-1")).lower()
            vs = v if isinstance(v, str) else v.decode("latin-1")
            seen_exact.add((kl, vs))
            norm_headers.append((kl.encode("latin-1"), vs.encode("latin-1")))
    for k, v in raw_headers:
        kl = (k if isinstance(k, str) else k.decode("latin-1")).lower()
        vs = v if isinstance(v, str) else v.decode("latin-1")
        if (kl, vs) in seen_exact:
            continue
        seen_exact.add((kl, vs))
        norm_headers.append((kl.encode("latin-1"), vs.encode("latin-1")))
    # StreamingResponse / SSE: iterate ``body_iterator`` and emit
    # multiple ``http.response.body`` frames with ``more_body=True``.
    # Handles both sync and async iterables.
    body_iter = getattr(response, "body_iterator", None)
    if body_iter is not None:
        await send({
            "type": "http.response.start",
            "status": status_code,
            "headers": norm_headers,
        })
        import inspect as _insp_send
        # ``await asyncio.sleep(0)`` between chunks yields control to
        # the event loop so a ``task.cancel()`` issued from the
        # client side (TestClient stream early-exit, real-client
        # disconnect) can propagate as ``CancelledError``. Without
        # this, a sync generator that runs in a tight loop (the
        # ``while True: yield b'x'`` shape) never gives the
        # scheduler a chance to deliver the cancellation, and
        # ``cli.stream(...)`` exit hangs indefinitely.
        if hasattr(body_iter, "__anext__") or _insp_send.isasyncgen(body_iter):
            async for chunk in body_iter:
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                await send({
                    "type": "http.response.body",
                    "body": bytes(chunk),
                    "more_body": True,
                })
                await asyncio.sleep(0)
        else:
            for chunk in body_iter:
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                await send({
                    "type": "http.response.body",
                    "body": bytes(chunk),
                    "more_body": True,
                })
                await asyncio.sleep(0)
        await send({
            "type": "http.response.body",
            "body": b"",
            "more_body": False,
        })
        return

    body = getattr(response, "body", b"") or b""
    if not isinstance(body, (bytes, bytearray)):
        # Unknown body shape — try a str fallback or bail.
        if isinstance(body, str):
            body = body.encode("utf-8")
        else:
            raise NotImplementedError(
                f"in-process ASGI: cannot serialise body of type {type(body).__name__}"
            )
    await send({
        "type": "http.response.start",
        "status": status_code,
        "headers": norm_headers,
    })
    await send({
        "type": "http.response.body",
        "body": bytes(body),
    })


async def _dispatch_to_subapp_route(subapp, request):
    """Match ``request.url.path`` against the sub-app's registered
    routes and invoke the matched endpoint directly. Used by
    ``app.host()`` forwarding — bypasses the sub-app's ASGI entry (and
    its Rust-server startup path) so dispatch completes in-process.
    """
    import re as _re_local
    from fastapi_turbo.responses import JSONResponse as _JR

    path = request.url.path
    method = request.method.upper()
    matched_route = None
    matched_params: dict = {}

    for route in getattr(subapp.router, "routes", []):
        route_path = getattr(route, "path", None)
        route_methods = getattr(route, "methods", None) or set()
        if not route_path:
            continue
        if method not in {m.upper() for m in route_methods}:
            continue
        # Compile ``/a/{id}/b/{name:path}`` into a regex on first use,
        # cached on the route object.
        regex = getattr(route, "_fastapi_turbo_host_regex", None)
        if regex is None:
            pattern = "^"
            idx = 0
            for m in _re_local.finditer(
                r"\{([^{}:]+)(?::([^{}]+))?\}", route_path,
            ):
                pattern += _re_local.escape(route_path[idx:m.start()])
                pname = m.group(1)
                conv = m.group(2)
                if conv == "path":
                    pattern += f"(?P<{pname}>.+)"
                else:
                    pattern += f"(?P<{pname}>[^/]+)"
                idx = m.end()
            pattern += _re_local.escape(route_path[idx:]) + "$"
            regex = _re_local.compile(pattern)
            try:
                route._fastapi_turbo_host_regex = regex  # type: ignore[attr-defined]
            except (AttributeError, TypeError):
                pass
        match = regex.match(path)
        if match is None:
            continue
        matched_route = route
        matched_params = match.groupdict()
        break

    if matched_route is None:
        return _JR(content={"detail": "Not Found"}, status_code=404)

    endpoint = matched_route.endpoint
    # Refine Sentry transaction with the sub-app's endpoint so tests
    # asserting on ``event["transaction"]`` see ``/subapp`` (url) or
    # the endpoint's qualified name (component).
    try:
        orig_ep = getattr(endpoint, "_fastapi_turbo_original_endpoint", endpoint)
        _refine_sentry_transaction(orig_ep, matched_route.path)
    except Exception as _exc:  # noqa: BLE001
        _log.debug("silent catch in applications: %r", _exc)

    # Coerce path params to the endpoint's annotated types.
    import inspect as _inspect_local
    try:
        sig = _inspect_local.signature(endpoint)
    except (TypeError, ValueError):
        sig = None
    call_kwargs = dict(matched_params)
    if sig is not None:
        for pname, p in sig.parameters.items():
            if pname in call_kwargs:
                ann = p.annotation
                if ann is int:
                    try:
                        call_kwargs[pname] = int(call_kwargs[pname])
                    except (ValueError, TypeError):
                        pass
                elif ann is float:
                    try:
                        call_kwargs[pname] = float(call_kwargs[pname])
                    except (ValueError, TypeError):
                        pass

    try:
        if _inspect_local.iscoroutinefunction(endpoint):
            result = await endpoint(**call_kwargs)
        else:
            result = endpoint(**call_kwargs)
    except Exception as exc:  # noqa: BLE001
        from fastapi_turbo.exceptions import HTTPException as _HE
        if isinstance(exc, _HE):
            return _JR(content={"detail": exc.detail}, status_code=exc.status_code)
        return _JR(content={"detail": "Internal Server Error"}, status_code=500)

    if hasattr(result, "status_code"):
        return result
    if isinstance(result, (dict, list)) or result is None:
        return _JR(content=result)
    return _JR(content=result)



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
        worker_timeout: float | None = None,
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
        # ``worker_timeout`` bounds how long a single async handler may
        # block the shared worker loop before we cancel its task and
        # raise ``TimeoutError``. Default None — matches FastAPI's "no
        # framework-imposed timeout" behaviour. Also overridable per
        # process via ``FASTAPI_TURBO_WORKER_TIMEOUT`` env var.
        self.worker_timeout: float | None = worker_timeout
        # Expose the instance so ``_async_worker._default_timeout`` can
        # pick up the per-app setting without needing it plumbed through
        # every submit call site. Last-constructed wins — single-app
        # processes are the common case.
        type(self)._fastapi_turbo_current_instance = self  # type: ignore[attr-defined]
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
        # Raw ASGI-3 middleware classes registered via ``add_middleware``.
        # The HTTP-shim list above adapts these per-request; this list
        # preserves them so ``_start_lifespan_mw_chain`` can dispatch a
        # ``lifespan`` scope through the same chain (Sentry/OTel need it).
        self._raw_asgi_middlewares: list[tuple[type, dict[str, Any]]] = []
        # Registration-order log spanning BOTH Tower-bound markers
        # (CORS/GZip/HTTPSRedirect) and raw ASGI middlewares. The
        # in-process dispatcher uses this to compose the chain in
        # the order the user called ``add_middleware``, so a custom
        # ASGI middleware added AFTER ``HTTPSRedirectMiddleware``
        # correctly wraps the redirect response. Each entry:
        # ``("tower"|"raw", middleware_cls, kwargs, seq)``.
        self._mw_registration_log: list[
            tuple[str, type, dict[str, Any], int]
        ] = []
        self._mw_registration_seq: int = 0
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

        # Sentry's ``FastApiIntegration`` / ``StarletteIntegration``
        # install by monkey-patching ``Starlette.__call__`` so every
        # request gets wrapped in ``SentryAsgiMiddleware``. Our Rust
        # server bypasses ``app.__call__``, so that patch never fires.
        # Auto-install ``SentryAsgiMiddleware`` here whenever a Sentry
        # client with one of those integrations is already active, so
        # the tracing / error-capture path works end-to-end.
        _ensure_sentry_middleware(self)

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
        """Dispatch requests matching the ``Host`` header to a sub-app.

        When a request's ``Host`` header (or its leading label for wildcard
        patterns) matches ``hostname``, the request is forwarded to
        ``app`` — typically another FastAPI instance. Matches Starlette's
        ``Host`` routing semantics.

        Install a one-time HTTP middleware that forwards matching
        requests by invoking the sub-app's ASGI entry and returning its
        response. The check is a dict lookup (~100ns per request); the
        actual forwarding only fires when the Host header matches.
        """
        if not hasattr(self, "_hosts"):
            self._hosts: list[tuple[str, Any, str | None]] = []
        self._hosts.append((hostname, app, name))

        # Install the host-dispatch middleware on first call.
        if not getattr(self, "_host_dispatcher_installed", False):
            self._host_dispatcher_installed = True
            _app_ref = self

            def _match_host(host_header: str):
                """Return (subapp, stripped_host) if the header matches
                any registered host, else None. Supports both exact
                match and Starlette's ``subapp`` → ``subapp`` form (no
                dot in hostname) or ``subapp.example.com`` form."""
                if not host_header:
                    return None
                # Strip port.
                hs = host_header.split(":", 1)[0].lower()
                for entry in _app_ref._hosts:
                    hn = entry[0].lower()
                    sub = entry[1]
                    if sub is None:
                        continue
                    if hn == hs:
                        return sub
                    # ``subapp`` hostname matches both ``subapp`` and
                    # ``subapp.foo.com`` — Starlette treats the first
                    # label as the match when the stored host has no
                    # dot. Starlette itself uses a regex, but this
                    # label-match covers the common cases.
                    if "." not in hn and hs.split(".", 1)[0] == hn:
                        return sub
                return None

            async def _host_dispatch(request, call_next):
                host_header = request.headers.get("host", "")
                subapp = _match_host(host_header)
                if subapp is None:
                    return await call_next(request)
                # Match the request against the sub-app's Python-side
                # route list and invoke the matched endpoint directly.
                # We don't go through the sub-app's ASGI ``__call__``
                # because that would try to spin up a second Rust
                # server and deadlock under TestClient.
                return await _dispatch_to_subapp_route(subapp, request)

            # Install as the OUTERMOST middleware so the host check
            # happens before CORS / Sentry / etc. Starlette's HostRouter
            # sits at the top of the app stack.
            self._http_middlewares.insert(0, _host_dispatch)

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
            from fastapi_turbo.exceptions import FastAPIError as _FE
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

            # default_response_class cascade — see the equivalent
            # block in routing.py for the inheritance rules. The
            # walker threads ``parent_default`` so nested routers
            # without their own default still pick up an ancestor's.
            outer_default = (
                default_response_class
                if default_response_class is not None
                else getattr(router, "default_response_class", None)
            )

            # Outer-most include kwarg deps + the included router's
            # own deps — every route below this include sees these
            # before its own/intermediate deps.
            outer_extra_deps = (
                list(dependencies or [])
                + list(getattr(router, "dependencies", []) or [])
            )

            def _mirror(src_router, pfx: str, parent_default, parent_deps) -> None:
                own_default = getattr(src_router, "default_response_class", None)
                eff_default = (
                    own_default if own_default is not None else parent_default
                )
                # ``parent_deps`` is the deps chain accumulated from
                # the outermost include down to (but not including)
                # this router's own deps. Routes on THIS router get
                # ``parent_deps`` (already includes outer include
                # kwargs + ancestor router deps + intermediate include
                # kwargs from upstream callers). The current router's
                # own ``.dependencies`` are appended for the routes
                # registered directly on it.
                eff_extra_deps = list(parent_deps)
                eff_extra_deps.extend(
                    getattr(src_router, "dependencies", []) or []
                )
                for r in getattr(src_router, "routes", []):
                    if getattr(r, "_is_included_shadow", False):
                        continue
                    clone = _copy.copy(r)
                    clone.path = _stack_path(pfx, getattr(r, "path", ""))
                    clone._is_included_shadow = True
                    if (
                        eff_default is not None
                        and getattr(clone, "response_class", None) is None
                        and getattr(
                            clone,
                            "_fastapi_turbo_effective_response_class",
                            None,
                        )
                        is None
                    ):
                        clone._fastapi_turbo_effective_response_class = (
                            eff_default
                        )
                    if eff_extra_deps:
                        clone._fastapi_turbo_include_deps = list(eff_extra_deps)
                    # Stamp the owning router so the in-process
                    # dispatcher can resolve closest-wins precedence
                    # for ``strict_content_type`` (and any other
                    # router-level setting) at request time without
                    # having to walk the include tree.
                    clone._fastapi_turbo_owner_router = src_router
                    self.router.routes.append(clone)
                for entry in getattr(src_router, "_included_routers", []):
                    child_router, child_prefix = entry[0], entry[1]
                    child_meta = entry[3] if len(entry) >= 4 else {}
                    child_include_default = (
                        child_meta.get("default_response_class")
                        if isinstance(child_meta, dict)
                        else None
                    )
                    nested_default = (
                        child_include_default
                        if child_include_default is not None
                        else eff_default
                    )
                    # Carry forward parent + this router's own deps +
                    # this child include's kwarg deps. The child router's
                    # OWN deps are added by the recursive call's
                    # ``eff_extra_deps`` extension.
                    child_include_deps = (
                        list(child_meta.get("dependencies", []) or [])
                        if isinstance(child_meta, dict)
                        else []
                    )
                    nested_parent_deps = (
                        list(eff_extra_deps) + child_include_deps
                    )
                    nested = _stack_path(
                        _stack_path(pfx, child_prefix or ""),
                        getattr(child_router, "prefix", "") or "",
                    )
                    _mirror(
                        child_router,
                        nested,
                        nested_default,
                        nested_parent_deps,
                    )

            # Outer-most call: ``parent_deps`` is the include kwargs
            # only — the included router's OWN deps are added by
            # ``_mirror`` for each of its routes.
            _mirror(
                router,
                full_prefix,
                outer_default,
                list(dependencies or []),
            )
        except Exception as _exc:  # noqa: BLE001
            _log.debug("silent catch in applications: %r", _exc)

    # ------------------------------------------------------------------
    # Middleware
    # ------------------------------------------------------------------

    def _keep_sentry_outermost(self) -> None:
        """Reorder ``_http_middlewares`` so ``SentryAsgiMiddleware`` is
        the last element (runtime-outermost after the reverse in
        ``_wrap_with_http_middlewares``).

        Stock Sentry monkey-patches ``Starlette.__call__`` — the patched
        entry always wraps everything. Our auto-install adds
        ``SentryAsgiMiddleware`` at ``FastAPI.__init__`` time (before
        any user middleware), so subsequent ``add_middleware`` calls
        would bury it. This reorder preserves Sentry's outermost
        placement regardless of add order.
        """
        try:
            from sentry_sdk.integrations.asgi import SentryAsgiMiddleware  # noqa: PLC0415
        except ImportError:
            return
        lst = getattr(self, "_http_middlewares", None)
        if not lst:
            return
        sentry_entries: list = []
        others: list = []
        for entry in lst:
            # Entries may be raw callables (our _shim closures), class
            # instances, or functions. Inspect attributes to detect
            # whether this item wraps ``SentryAsgiMiddleware``.
            is_sentry = False
            if isinstance(entry, SentryAsgiMiddleware):
                is_sentry = True
            else:
                mw_cls = getattr(entry, "__fastapi_turbo_mw_cls", None)
                if mw_cls is SentryAsgiMiddleware:
                    is_sentry = True
            if is_sentry:
                sentry_entries.append(entry)
            else:
                others.append(entry)
        lst[:] = others + sentry_entries

    def add_middleware(self, middleware_cls, **kwargs: Any) -> None:
        """Register a middleware class. Delegates to the internal impl,
        then reorders so SentryAsgiMiddleware (if auto-installed) stays
        runtime-outermost regardless of add order."""
        try:
            self._add_middleware_impl(middleware_cls, **kwargs)
        finally:
            self._keep_sentry_outermost()

    def _add_middleware_impl(self, middleware_cls, **kwargs: Any) -> None:
        """Internal: register a middleware class without the Sentry
        reorder. Direct callers (internal auto-install paths) can use
        this if they've already arranged ordering.

        Handles three cases:
        1. Known Rust/Tower middleware (CORS, GZip, etc.) → Rust stack
        2. Python HTTP middleware (our marker) → per-handler chain
        3. BaseHTTPMiddleware subclass (Qwen pattern) → converted to
           @app.middleware("http") callable via its dispatch() method
        """
        # String shorthand: ``app.add_middleware("cors", ...)`` /
        # ``add_middleware("gzip", ...)`` etc. Record on the
        # middleware stack AND the registration log so the
        # in-process dispatcher's resolver can find it.
        if isinstance(middleware_cls, str):
            self._middleware_stack.append((middleware_cls, kwargs))
            self._mw_registration_seq += 1
            self._mw_registration_log.append(
                ("tower", middleware_cls, kwargs, self._mw_registration_seq)
            )
            _resolve_tower_bound_to_asgi_class(middleware_cls)
            return

        mw_type = getattr(middleware_cls, "_fastapi_turbo_middleware_type", None)
        if mw_type and mw_type.startswith("python_http_"):
            try:
                instance = middleware_cls(app=self, **kwargs)
            except TypeError:
                instance = middleware_cls(**kwargs)
            self._http_middlewares.append(instance)
            return

        # Rust/Tower-bound middleware (CORS/GZip/TrustedHost/HTTPSRedirect)
        # carries a known Tower-side ``_fastapi_turbo_middleware_type``.
        # Record on ``_middleware_stack`` so ``_build_middleware_config``
        # maps it to the matching Tower layer — do NOT fall through to
        # the generic ASGI shim (the class has no __call__ on instances
        # and exists purely as a marker for the Rust side). Exclude
        # ``base_http`` — that's the BaseHTTPMiddleware marker handled
        # in the branch below (dispatch()-based, NOT Tower-bound).
        # TrustedHost intentionally excluded — it runs through the
        # Python ASGI chain so SentryAsgiMiddleware (wrapping around)
        # observes the request and can emit a transaction span for
        # host-rejected requests. The ~1μs overhead vs the Tower layer
        # is worth the tracing parity.
        _TOWER_BOUND_TYPES = {"cors", "gzip", "httpsredirect"}
        if mw_type in _TOWER_BOUND_TYPES:
            self._middleware_stack.append((middleware_cls, kwargs))
            self._mw_registration_seq += 1
            self._mw_registration_log.append(
                ("tower", middleware_cls, kwargs, self._mw_registration_seq)
            )
            # Pre-load the real Starlette substitute NOW so the
            # in-process dispatcher never has to touch ``sys.modules``
            # at request time (avoids a race in concurrent ASGI /
            # serverless environments where another thread might be
            # mid-import of starlette.* modules).
            _resolve_tower_bound_to_asgi_class(middleware_cls)
            return

        # BaseHTTPMiddleware subclass — Qwen uses this for auth middleware.
        # Convert to an @app.middleware("http") function by wrapping dispatch().
        from fastapi_turbo.middleware.base import BaseHTTPMiddleware
        if isinstance(middleware_cls, type) and issubclass(middleware_cls, BaseHTTPMiddleware):
            try:
                instance = middleware_cls(app=self, **kwargs)
            except TypeError:
                instance = middleware_cls(**kwargs)

            async def _dispatch_wrapper(request, call_next, _inst=instance):
                return await _inst.dispatch(request, call_next)

            self._http_middlewares.append(_dispatch_wrapper)
            return

        # Generic ASGI middleware class — the class constructor takes
        # ``app`` as the first argument and instances are ASGI3 callables
        # ``async def __call__(self, scope, receive, send)``.  Bridge it
        # through an ``@app.middleware("http")`` shim: build a minimal
        # ASGI scope from the ``Request``, drive ``instance(scope, receive,
        # send)`` where the inner ``app`` proxies to ``call_next`` (thus
        # letting the middleware wrap ``receive`` and observe the body).
        if (
            isinstance(middleware_cls, type)
            and hasattr(middleware_cls, "__call__")
        ):
            import inspect as _insp
            try:
                _sig = _insp.signature(middleware_cls.__init__)
                _accepts_app = "app" in _sig.parameters
            except (TypeError, ValueError):
                _accepts_app = False
            if _accepts_app:
                self._http_middlewares.append(
                    _make_asgi_middleware_shim(middleware_cls, kwargs)
                )
                # Also preserve the raw class for lifespan-scope dispatch.
                self._raw_asgi_middlewares.append((middleware_cls, kwargs))
                self._mw_registration_seq += 1
                self._mw_registration_log.append(
                    ("raw", middleware_cls, kwargs, self._mw_registration_seq)
                )
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
        from fastapi_turbo.middleware.trustedhost import TrustedHostMiddleware
        from fastapi_turbo.middleware.httpsredirect import HTTPSRedirectMiddleware

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
            elif hasattr(cls, "_fastapi_turbo_middleware_type"):
                # fastapi-turbo middleware class with a known Tower mapping
                config.append({"type": cls._fastapi_turbo_middleware_type, **kwargs})
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
        from fastapi_turbo.requests import Request
        scope = _current_request_scope.get() or {}
        request = Request({**scope, "type": "http", "app": self})
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
        # Sentry's ``StarletteIntegration.failed_request_status_codes``
        # asks us to capture HTTPException events when the status falls
        # in the configured set (default: [500..599]). Stock Starlette
        # routes through ExceptionMiddleware where Sentry's monkey-patch
        # lives; our dispatch doesn't, so emit the event ourselves.
        _maybe_sentry_capture_failed_request(exc)
        handler = self._lookup_exception_handler(exc)
        if handler is None:
            return None
        from fastapi_turbo.requests import Request
        scope = _current_request_scope.get() or {}
        request = Request({**scope, "type": "http", "app": self})
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
        from fastapi_turbo.dependencies import Depends as _Depends
        from fastapi_turbo.websockets import WebSocket as _WebSocket, WebSocketState as _WSState
        from fastapi_turbo.exceptions import WebSocketException as _WSExc

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
        except Exception as _exc:  # noqa: BLE001
            _log.debug("silent catch in applications: %r", _exc)
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
            if isinstance(raw_ann, str) and raw_ann in ("WebSocket", "fastapi_turbo.websockets.WebSocket"):
                return True
            return False

        from fastapi_turbo.param_functions import (
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
                except Exception as _exc:  # noqa: BLE001
                    _log.debug("silent catch in applications: %r", _exc)
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
                from fastapi_turbo.exceptions import (
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
            except Exception as _exc:  # noqa: BLE001
                _log.debug("silent catch in applications: %r", _exc)
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
                    from fastapi_turbo.exceptions import FastAPIError as _FE
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
            except Exception as _exc:  # noqa: BLE001
                _log.debug("silent catch in applications: %r", _exc)
                dep_hints = {}

            dep_kwargs: dict = {}
            if dep_sig is not None:
                try:
                    from fastapi_turbo.requests import HTTPConnection as _HTTPConn
                except ImportError:
                    _HTTPConn = None
                for p_name, p in dep_sig.parameters.items():
                    ann = dep_hints.get(p_name, p.annotation)
                    raw = p.annotation
                    # WebSocket / HTTPConnection injection. WS deps can
                    # accept either ``WebSocket`` or its parent
                    # ``HTTPConnection`` (Starlette parity — FA apps
                    # often inject ``HTTPConnection`` so one dep works
                    # for HTTP + WS routes alike).
                    if (
                        ann is _WebSocket
                        or (isinstance(ann, type) and issubclass(ann, _WebSocket))
                        or (
                            _HTTPConn is not None
                            and isinstance(ann, type)
                            and issubclass(ann, _HTTPConn)
                        )
                        or (
                            isinstance(raw, str)
                            and raw in (
                                "WebSocket",
                                "fastapi_turbo.websockets.WebSocket",
                                "HTTPConnection",
                                "fastapi_turbo.requests.HTTPConnection",
                            )
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

                    # Plain-typed param without marker → Query (FA default,
                    # matching the handler-level fallback at _wrap_websocket_
                    # endpoint's param_spec build). Lets
                    # ``def dep(token: str = "")`` pull ``token`` from
                    # ``?token=...`` on the connect URL.
                    if p.kind in (
                        _inspect.Parameter.VAR_POSITIONAL,
                        _inspect.Parameter.VAR_KEYWORD,
                    ):
                        continue
                    default_val = (
                        _inspect.Parameter.empty
                        if p.default is _inspect.Parameter.empty
                        else p.default
                    )
                    raw_val = ws.query_params.get(p_name)
                    if raw_val is None:
                        if default_val is not _inspect.Parameter.empty:
                            dep_kwargs[p_name] = default_val
                            continue
                        raise _WSExc(
                            code=1008,
                            reason=f"missing query parameter {p_name!r}",
                        )
                    if ann is _inspect.Parameter.empty or ann is None:
                        dep_kwargs[p_name] = raw_val
                    else:
                        dep_kwargs[p_name] = _validate_scalar(
                            raw_val, _inner_type(ann), p_name, "Query",
                        )

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
                from fastapi_turbo.exceptions import WebSocketDisconnect as _WD
                app_ref._ws_server_exceptions.append(_WD(code=code, reason=reason))
            except Exception as _exc:  # noqa: BLE001
                _log.debug("silent catch in applications: %r", _exc)
            if ws.application_state == _WSState.CONNECTING:
                ws._reject(403)
                return
            try:
                ws._ws.close(code, reason)
            except Exception as _exc:  # noqa: BLE001
                _log.debug("silent catch in applications: %r", _exc)

        def _capture_server_exception(exc):
            """Push onto the app's capture queues so TestClient can
            re-raise on session close."""
            try:
                app_ref._ws_server_exceptions.append(exc)
            except Exception as _exc:  # noqa: BLE001
                _log.debug("silent catch in applications: %r", _exc)

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
            from fastapi_turbo.compat import fastapi_shim as _fa_shim
            _APIWSRoute = getattr(
                getattr(_fa_shim, "fastapi_routing", None) or object(),
                "APIWebSocketRoute",
                None,
            )
        except Exception as _exc:  # noqa: BLE001
            _log.debug("silent catch in applications: %r", _exc)
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
            except Exception as _exc:  # noqa: BLE001
                _log.debug("silent catch in applications: %r", _exc)
            # Replay the TestClient's captured contextvars. This lets
            # ``ContextVar``-based state set in the test thread
            # (e.g. ``global_context.set({}); gs = global_context.get()``)
            # be observable from the handler/teardown that runs on the
            # server's async worker thread — mutations to values
            # retrieved via ``.get()`` from within replayed vars mutate
            # the SAME objects the test holds a reference to.
            try:
                q = getattr(app_ref, "_ws_pending_test_contexts", None)
                if q:
                    try:
                        test_ctx = q.pop(0)
                    except IndexError:
                        test_ctx = None
                    if test_ctx is not None:
                        for _var, _val in test_ctx.items():
                            try:
                                _var.set(_val)
                            except Exception as _exc:  # noqa: BLE001
                                _log.debug("silent catch in applications: %r", _exc)
            except Exception as _exc:  # noqa: BLE001
                _log.debug("silent catch in applications: %r", _exc)
            # WS middleware chain (Starlette-style ``Middleware(cls)`` where
            # ``cls`` is a factory: ``cls(app) -> wrapped_app``). FA parity:
            # tests register a ``websocket_middleware`` that wraps the
            # app in a ``try/except`` and calls ``websocket.close(code)``
            # on error. Build the chain here so the innermost "app" calls
            # the real handler logic; the middleware sees a
            # ``WebSocket(scope, receive, send)`` it can close via our
            # ``send``-bridge.
            ws_mw_factories = []
            try:
                for _cls, _kw in getattr(app_ref, "_middleware_stack", []):
                    if callable(_cls) and not isinstance(_cls, type):
                        ws_mw_factories.append((_cls, _kw))
            except Exception as _exc:  # noqa: BLE001
                _log.debug("silent catch in applications: %r", _exc)

            generators: list = []

            async def _run_handler_inner():
                nonlocal generators
                try:
                    kwargs, generators = await _build_kwargs(ws, path_kwargs)
                except _WSExc as _vexc:
                    # FA parity: validation-origin WebSocketException
                    # (e.g. missing required Header) is handled
                    # internally — close the WS with its code but do
                    # NOT let user WS middleware observe it as a raised
                    # error (test_depend_validation asserts the
                    # middleware never catches it).
                    _handle_ws_exc(ws, _vexc)
                    return
                if is_async_endpoint:
                    # Fast path: drive the user handler on the current
                    # thread via ``coro.send``. Works when the handler
                    # only awaits our ChannelAwaitable (thread-safe,
                    # releases GIL via py.detach). Fails with
                    # ``RuntimeError: no running event loop`` when the
                    # user calls real asyncio primitives
                    # (``asyncio.sleep(delay)``, ``asyncio.wait``, etc.)
                    # — in that case re-run on the shared async worker
                    # loop where ``get_running_loop()`` resolves.
                    try:
                        await endpoint(**kwargs)
                    except RuntimeError as _rt_exc:
                        msg = str(_rt_exc)
                        if (
                            "no running event loop" in msg
                            or "no current event loop" in msg
                        ):
                            from fastapi_turbo._async_worker import (
                                submit as _w_submit,
                            )
                            _w_submit(endpoint(**kwargs), app=app_ref)
                        else:
                            raise
                else:
                    endpoint(**kwargs)

            async def _invoke_with_middleware():
                if not ws_mw_factories:
                    await _run_handler_inner()
                    return
                # Inner ASGI app — delegates to handler, re-raises errors
                # so middleware can observe/catch them.
                async def _inner_app(scope, receive, send):
                    await _run_handler_inner()
                # Bridge send messages to the real ws
                async def _bridge_send(message):
                    mt = message.get("type", "")
                    if mt == "websocket.close":
                        code = message.get("code", 1000)
                        reason = message.get("reason", "") or ""
                        try:
                            await ws.close(code=code, reason=reason)
                        except Exception as _exc:  # noqa: BLE001
                            _log.debug("silent catch in applications: %r", _exc)
                        # Push a ``WebSocketDisconnect`` so the
                        # TestClient's ``__exit__`` surfaces the close
                        # code to ``pytest.raises(WebSocketDisconnect)``.
                        try:
                            from fastapi_turbo.exceptions import (
                                WebSocketDisconnect as _WD_MW,
                            )
                            app_ref._ws_server_exceptions.append(
                                _WD_MW(code=code, reason=reason)
                            )
                        except Exception as _exc:  # noqa: BLE001
                            _log.debug("silent catch in applications: %r", _exc)
                    elif mt == "websocket.accept":
                        try:
                            await ws.accept(
                                subprotocol=message.get("subprotocol"),
                                headers=message.get("headers"),
                            )
                        except Exception as _exc:  # noqa: BLE001
                            _log.debug("silent catch in applications: %r", _exc)
                    elif mt == "websocket.send":
                        if message.get("text") is not None:
                            try:
                                await ws.send_text(message["text"])
                            except Exception as _exc:  # noqa: BLE001
                                _log.debug("silent catch in applications: %r", _exc)
                        elif message.get("bytes") is not None:
                            try:
                                await ws.send_bytes(bytes(message["bytes"]))
                            except Exception as _exc:  # noqa: BLE001
                                _log.debug("silent catch in applications: %r", _exc)
                async def _bridge_receive():
                    try:
                        return await ws.receive()
                    except Exception as _exc:  # noqa: BLE001
                        _log.debug("silent catch in applications: %r", _exc)
                        return {"type": "websocket.disconnect", "code": 1000}
                # Build the chain outermost-first: final_app wraps each.
                current_app = _inner_app
                # Reverse: the first middleware added should be outermost.
                for cls, kw in reversed(ws_mw_factories):
                    try:
                        current_app = cls(current_app, **kw)
                    except TypeError:
                        try:
                            current_app = cls(current_app)
                        except Exception as _exc:  # noqa: BLE001
                            _log.debug("silent catch in applications: %r", _exc)
                mw_scope = ws.scope if isinstance(ws.scope, dict) else {"type": "websocket"}
                await current_app(mw_scope, _bridge_receive, _bridge_send)

            try:
                await _invoke_with_middleware()
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
                    from fastapi_turbo.exceptions import (
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
                        except Exception as _exc:  # noqa: BLE001
                            _log.debug("silent catch in applications: %r", _exc)
                    # Close with 1008 policy-violation regardless of what
                    # the handler did.
                    try:
                        from fastapi_turbo.exceptions import (
                            WebSocketDisconnect as _WD,
                        )
                        app_ref._ws_server_exceptions.append(
                            _WD(code=1008, reason="validation error")
                        )
                    except Exception as _exc:  # noqa: BLE001
                        _log.debug("silent catch in applications: %r", _exc)
                    if ws.application_state == _WSState.CONNECTING:
                        ws._reject(403)
                    else:
                        try:
                            ws._ws.close(1008, "validation error")
                        except Exception as _exc:  # noqa: BLE001
                            _log.debug("silent catch in applications: %r", _exc)
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
                                from fastapi_turbo.exceptions import (
                                    WebSocketDisconnect as _WD,
                                )
                                last = getattr(ws, "_last_close_code", None) or 1000
                                last_reason = getattr(ws, "_last_close_reason", "") or ""
                                app_ref._ws_server_exceptions.append(
                                    _WD(code=last, reason=last_reason)
                                )
                            except Exception as _exc:  # noqa: BLE001
                                _log.debug("silent catch in applications: %r", _exc)
                        except Exception as _exc:  # noqa: BLE001
                            _log.debug("silent catch in applications: %r", _exc)
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
                        except Exception as _exc:  # noqa: BLE001
                            _log.debug("silent catch in applications: %r", _exc)
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

        # If raw ASGI middleware is registered, dispatch the WS invocation
        # through the composed MW chain so middlewares that key off
        # ``scope['type'] == 'websocket'`` (Sentry's connection-span, OTel
        # tracing, rate-limit gates, logging) see the connection, can
        # wrap receive/send, and can capture exceptions from the user
        # handler via ``try/except await self.app(scope, receive, send)``.
        app_self = self

        async def _ws_asgi_chain_entry(ws, **path_params):
            # Fast path: no ASGI MW registered — behaviour identical to
            # the pre-chain path.
            if not app_self._raw_asgi_middlewares:
                return await _ws_entry(ws, **path_params)
            return await _ws_entry_with_asgi_chain(app_self, ws, path_params, _ws_entry)

        # Forward the WS-synthetic-route + endpoint_ctx attrs that
        # route collection relies on for OpenAPI / mount-prefix logic.
        _ws_asgi_chain_entry._ws_synthetic_route = _synthetic_route  # type: ignore[attr-defined]
        _ws_asgi_chain_entry._ws_endpoint_ctx = _ws_endpoint_ctx  # type: ignore[attr-defined]

        # Always return an async entry: Rust treats both sync/async the
        # same way via the worker loop, and this lets us await deps and
        # teardown uniformly even for sync endpoints.
        return _ws_asgi_chain_entry

    def _get_all_dependencies_for_route(
        self, router: APIRouter, route, include_deps: list | None = None,
    ) -> list:
        """Merge app-level, include-level, router-level, and route-level dependencies."""
        # FA parity: the ``/openapi.json`` / ``/docs`` endpoints bypass
        # ALL user-registered dependencies — the docs should never
        # require app-level auth headers to fetch the schema.
        if getattr(route, "_fastapi_turbo_bypass_deps", False):
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
        from fastapi_turbo.datastructures import DefaultPlaceholder as _DP
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
                    custom_ep._fastapi_turbo_route_obj = route  # type: ignore[attr-defined]
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
                    "name": "__fastapi_turbo_override_request__",
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
                        except Exception as _exc:  # noqa: BLE001
                            _log.debug("silent catch in applications: %r", _exc)
                            _item_adapter = None

                _app_for_stream = self

                def _json_lines_wrap(
                    _orig=_orig_endpoint, _is_a=_is_async_gen,
                    _ta=_item_adapter, _app=_app_for_stream, **kwargs,
                ):
                    from fastapi_turbo.responses import StreamingResponse as _SR
                    from fastapi_turbo.encoders import jsonable_encoder as _je
                    from fastapi_turbo.exceptions import (
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
                        from fastapi_turbo._resolution import _make_sync_wrapper
                        endpoint = _make_sync_wrapper(endpoint, for_handler=True, app=self)
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

            # Per-app ``worker_timeout`` plumbing for pure-async
            # endpoints: when the user has actually set a timeout AND
            # ``_raw_asgi_middlewares`` isn't carrying a shim that
            # already wrapped the handler, wrap in a thin sync caller
            # so ``submit(coro, app=app)`` is called with the owning
            # app. For the common case (no ``worker_timeout`` set),
            # skip the wrap — Rust's ``submit_to_async_worker`` then
            # handles the coro directly via its existing APP_INSTANCE
            # plumbing, saving a Python hop per request.
            needs_app_plumb = (
                is_async
                and inspect.iscoroutinefunction(endpoint)
                and not inspect.isasyncgenfunction(endpoint)
                and getattr(self, "worker_timeout", None) is not None
                and os.environ.get("FASTAPI_TURBO_WORKER_TIMEOUT") is None
            )
            if needs_app_plumb:
                from fastapi_turbo._resolution import _make_sync_wrapper
                endpoint = _make_sync_wrapper(endpoint, for_handler=True, app=self)
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
                        kwargs.pop("__fastapi_turbo_override_request__", None)
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
                        kwargs.pop("__fastapi_turbo_override_request__", None)
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
                    endpoint._fastapi_turbo_lax_content_type = True  # type: ignore[attr-defined]
                except (AttributeError, TypeError):
                    pass
            # Attach the original route object so Rust can populate
            # ``request.scope["route"]`` — ``test_route_scope`` asserts.
            try:
                endpoint._fastapi_turbo_route_obj = route  # type: ignore[attr-defined]
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
                            except Exception as _exc:  # noqa: BLE001
                                _log.debug("silent catch in applications: %r", _exc)
                    else:
                        # HTTP endpoints: patch the compiled handler's
                        # ``_fastapi_turbo_endpoint_ctx`` dict so
                        # ``RequestValidationError`` / ``ResponseValidationError``
                        # raised from a mounted sub-app surface the full
                        # mount-prefixed URL (``/sub/items/``) rather than
                        # the sub-app-internal path (``/items/``).
                        ep = r.get("endpoint")
                        if ep is not None:
                            try:
                                ctx = getattr(ep, "_fastapi_turbo_endpoint_ctx", None)
                                if isinstance(ctx, dict):
                                    ctx["path"] = r["path"]
                            except Exception as _exc:  # noqa: BLE001
                                _log.debug("silent catch in applications: %r", _exc)
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
            elif callable(mounted_app):
                # Arbitrary ASGI app (WSGIMiddleware, sub-ASGI, static
                # file server, etc.). Register a catch-all HTTP route
                # under ``<mount_path>/{__asgi_rest__:path}`` that proxies
                # through an ASGI shim — we materialise a Starlette scope,
                # drive the inner app, and stream its response back out
                # as a ``fastapi_turbo.responses.Response``.
                all_routes.extend(
                    self._build_asgi_mount_routes(mount_path, mounted_app)
                )

        return all_routes

    def _build_asgi_mount_routes(
        self, mount_path: str, asgi_app: Any
    ) -> list[dict[str, Any]]:
        """Build catch-all HTTP route entries that proxy requests under
        ``mount_path`` to ``asgi_app`` (the Starlette/ASGI app the user
        handed to ``app.mount``).  One entry is emitted per common HTTP
        method so axum's method router dispatches correctly."""
        mount_path_clean = mount_path.rstrip("/")

        async def _proxy(request: Any) -> Any:
            # Drive the inner ASGI app via a minimal scope + buffered
            # receive/send. Stream the resulting response back as a
            # fastapi_turbo Response.
            scope = dict(getattr(request, "scope", {}) or {})
            # Strip the mount prefix from the path so the inner app sees
            # requests relative to its own root (Starlette behaviour).
            full_path = scope.get("path", "")
            if mount_path_clean and full_path.startswith(mount_path_clean):
                inner_path = full_path[len(mount_path_clean):] or "/"
            else:
                inner_path = full_path or "/"
            scope = {
                **scope,
                "type": "http",
                "path": inner_path,
                "raw_path": inner_path.encode("latin-1"),
                "root_path": (scope.get("root_path", "") or "") + mount_path_clean,
            }
            body_bytes = await request.body()

            async def _receive():
                return {
                    "type": "http.request",
                    "body": body_bytes,
                    "more_body": False,
                }

            status_holder: dict[str, Any] = {"status": 200, "headers": []}
            body_parts: list[bytes] = []

            async def _send(message):
                mtype = message.get("type")
                if mtype == "http.response.start":
                    status_holder["status"] = message.get("status", 200)
                    status_holder["headers"] = list(message.get("headers") or [])
                elif mtype == "http.response.body":
                    chunk = message.get("body", b"") or b""
                    if chunk:
                        body_parts.append(chunk)

            # a2wsgi / uvloop transitively call the deprecated
            # ``asyncio.iscoroutinefunction`` on Python 3.14.  Tests that
            # set ``filterwarnings=error`` convert that into a runtime
            # exception for the inner app.  Suppress just that specific
            # deprecation for the duration of the proxied call.
            import warnings as _warnings
            with _warnings.catch_warnings():
                _warnings.filterwarnings(
                    "ignore",
                    message=r".*asyncio\.iscoroutinefunction.*",
                    category=DeprecationWarning,
                )
                await asgi_app(scope, _receive, _send)

            from fastapi_turbo.responses import Response as _Response
            resp = _Response(
                content=b"".join(body_parts),
                status_code=status_holder["status"],
            )
            # Replace the default headers with the inner app's — content-
            # type etc. must come from the mounted app, not our JSON
            # default.
            resp.headers.clear()
            resp.raw_headers.clear()
            for raw_k, raw_v in status_holder["headers"]:
                k = raw_k.decode("latin-1") if isinstance(raw_k, bytes) else str(raw_k)
                v = raw_v.decode("latin-1") if isinstance(raw_v, bytes) else str(raw_v)
                resp.headers.append(k, v)
            return resp

        _proxy.__name__ = f"__asgi_mount_{mount_path_clean.strip('/').replace('/', '_') or 'root'}__"

        # Explicit ``request`` parameter: Rust injects the Request object
        # and we forward it to the ASGI shim.
        from fastapi_turbo.requests import Request as _Req
        _proxy.__annotations__ = {"request": _Req}

        catchall_path = f"{mount_path_clean}/{{__asgi_rest__:path}}"
        root_path = mount_path_clean or "/"

        out: list[dict[str, Any]] = []
        # Emit both the exact-mount and catchall variants so ``GET
        # /v1`` and ``GET /v1/foo`` both dispatch to the proxy.
        for path_variant in (root_path, mount_path_clean or "/", catchall_path):
            # Dedupe while preserving order — the two leading entries
            # collapse when root_path has no extra prefix.
            if any(r["path"] == path_variant for r in out):
                continue
            for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
                out.append({
                    "path": path_variant,
                    "methods": [method],
                    "endpoint": _proxy,
                    "is_async": True,
                    "handler_name": _proxy.__name__,
                    "params": [{
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
                    }],
                    "is_websocket": False,
                    "include_in_schema": False,
                    "_from_mount": mount_path_clean,
                    "tags": [],
                })
        return out

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

        State machine via ``_startup_state``:

          * ``"not_started"`` (default): handlers haven't run yet.
            Calling this method runs them and transitions to
            ``"started"`` on success or ``"failed"`` on the first
            exception.
          * ``"started"``: handlers ran successfully. Re-entry is a
            no-op (matches Starlette / FastAPI: lifespan startup
            fires once per app instance per lifecycle).
          * ``"failed"``: a handler raised. Re-entry RE-RAISES a
            ``RuntimeError`` describing the original failure. The
            ASGI dispatcher checks this state on every request and
            refuses to serve traffic against a poisoned app
            (probe-confirmed bug: earlier impl set the "ran" flag
            before the handler completed, so the failed app
            silently served subsequent ``/ok`` requests with 200).
          * ``"running"``: re-entrant call from inside a handler.
            Treated as a programming bug; raises.

        ``_run_shutdown_handlers`` resets the state to
        ``"not_started"`` so a reused app instance can re-fire
        startup on the next lifespan / request.

        Earlier R-batches had two callers race to fire startup:
        ``_asgi_lifespan`` (driven by ASGITransport / TestClient)
        and ``_install_in_process_dynamic_routes`` (lazily called
        on first http request). Without the state machine,
        ``@app.on_event("startup")`` ran twice on the happy path
        AND failed apps kept serving traffic AND reused apps never
        re-ran startup.
        """
        state = getattr(self, "_startup_state", "not_started")
        if state == "started":
            return
        if state == "failed":
            cause = getattr(self, "_startup_failure", None)
            raise RuntimeError(
                "fastapi-turbo: startup handler raised earlier; the app "
                "is in a failed state and cannot serve traffic. Re-create "
                f"the app instance to retry. Original error: {cause!r}"
            )
        if state == "running":
            raise RuntimeError(
                "fastapi-turbo: re-entrant call to startup handlers; "
                "a startup hook is invoking another startup-running code "
                "path. This is a bug in the user's startup chain."
            )
        # state == "not_started" — fire handlers.
        self._startup_state = "running"
        from fastapi_turbo._async_worker import submit as _submit
        try:
            for handler in self._collect_startup_handlers():
                if inspect.iscoroutinefunction(handler):
                    _submit(handler(), app=self)
                else:
                    handler()
        except Exception as exc:
            self._startup_state = "failed"
            self._startup_failure = exc
            raise
        else:
            self._startup_state = "started"

    def _run_shutdown_handlers(self) -> None:
        """Execute all registered shutdown handlers on the worker loop.

        Resets ``_startup_state`` to ``"not_started"`` so a reused
        app instance can re-fire startup on the next lifespan or
        first http request — Starlette behaviour. Earlier impl
        left the started flag pinned, so two TestClient context
        managers on the same app produced startup=1 / shutdown=2
        (probe-confirmed). Now both run once per ``startup ↔
        shutdown`` cycle as upstream does."""
        from fastapi_turbo._async_worker import submit as _submit
        for handler in self._collect_shutdown_handlers():
            if inspect.iscoroutinefunction(handler):
                _submit(handler(), app=self)
            else:
                handler()
        # Reset for the next lifecycle.
        self._startup_state = "not_started"
        self._startup_failure = None
        # The dynamic-routes installer is also bound to this
        # lifecycle — clear its guard so a fresh lifespan
        # re-installs the docs routes (FastAPI 's openapi_schema
        # cache is reset elsewhere).
        self._in_process_dynamic_routes_installed = False

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

        from fastapi_turbo._async_worker import submit as _submit
        _submit(_enter_all(), app=self)

    def _run_lifespan_shutdown(self) -> None:
        """Exit every lifespan context manager in reverse-start order.

        Failures from ``__aexit__`` propagate — matches Starlette /
        upstream FastAPI's contract: a lifespan ctx-manager whose
        cleanup raises must surface that exception to the ASGI
        server (so the supervisor sees ``lifespan.shutdown.failed``
        and the operator gets the cleanup-error stack trace).
        Earlier impl swallowed every exception silently, breaking
        production observability.

        Best-effort across multiple ctx-managers: we still attempt
        every ctx's ``__aexit__`` (unwinding shouldn't stop on the
        first failure — at-most-once cleanup per resource matters
        more than abort-on-first-error). The first exception
        encountered is re-raised at the end."""
        cms = getattr(self, "_lifespan_cms", None)
        if not cms:
            return

        first_exc: list[Exception] = []

        async def _exit_all():
            for cm in reversed(cms):
                try:
                    await cm.__aexit__(None, None, None)
                except Exception as exc:  # noqa: BLE001
                    if not first_exc:
                        first_exc.append(exc)

        from fastapi_turbo._async_worker import submit as _submit
        _submit(_exit_all(), app=self)
        self._lifespan_cms = None
        if first_exc:
            raise first_exc[0]

    # --- async variants callable from inside the worker loop ---------
    # The sync `_run_*` helpers submit to the worker loop via `submit()`,
    # which would deadlock if invoked from inside a coroutine already
    # running on that loop (e.g. the lifespan-MW dispatcher below). These
    # coroutine variants do the work inline — same result, awaitable.
    async def _async_run_startup_handlers(self) -> None:
        for handler in self._collect_startup_handlers():
            if inspect.iscoroutinefunction(handler):
                await handler()
            else:
                handler()

    async def _async_run_shutdown_handlers(self) -> None:
        for handler in self._collect_shutdown_handlers():
            if inspect.iscoroutinefunction(handler):
                await handler()
            else:
                handler()

    async def _async_run_lifespan_startup(self) -> None:
        if getattr(self, "_lifespan_cms", None):
            return
        lifespans = self._collect_lifespans()
        if not lifespans:
            return
        from contextlib import asynccontextmanager as _acm
        import inspect as _inspect

        def _wrap(cb):
            def _probe():
                return cb(self)
            cm = _probe()
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
        for cm in cms:
            state = await cm.__aenter__()
            if state:
                merged.update(state)
        self._app_state = merged
        for k, v in merged.items():
            setattr(self.state, k, v)

    async def _async_run_lifespan_shutdown(self) -> None:
        """Async variant of ``_run_lifespan_shutdown``. Same
        propagation contract: best-effort across all ctx-managers,
        first ``__aexit__`` failure re-raised at the end so the
        ASGI lifespan dispatcher emits ``lifespan.shutdown.failed``
        upstream. Earlier impl silently swallowed every exception."""
        cms = getattr(self, "_lifespan_cms", None)
        if not cms:
            return
        first_exc: Exception | None = None
        for cm in reversed(cms):
            try:
                await cm.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                if first_exc is None:
                    first_exc = exc
        self._lifespan_cms = None
        if first_exc is not None:
            raise first_exc

    # --- lifespan dispatch through the raw ASGI middleware chain ----
    def _start_lifespan_mw_chain(self) -> bool:
        """If raw ASGI middleware is registered, dispatch a lifespan.startup
        message through the composed chain and block until complete.
        Returns True if dispatched (caller should use the chained path for
        shutdown too), False if there's no chain to drive (caller does the
        direct-call path).

        The chain lets Sentry/OpenTelemetry-style middleware that hooks
        ``scope['type'] == 'lifespan'`` see startup/shutdown events.
        """
        if not self._raw_asgi_middlewares:
            return False

        import asyncio
        import traceback
        from fastapi_turbo._async_worker import submit as _submit

        app_self = self
        state: dict = {
            "recv_q": None,
            "send_done": None,
            "send_events": [],
            "task": None,
        }

        async def _inner_app(scope, receive, send):
            if scope.get("type") != "lifespan":
                return
            msg = await receive()
            if msg.get("type") != "lifespan.startup":
                await send({
                    "type": "lifespan.startup.failed",
                    "message": f"unexpected message {msg.get('type')!r}",
                })
                return
            try:
                await app_self._async_run_lifespan_startup()
                await app_self._async_run_startup_handlers()
            except BaseException:  # noqa: BLE001
                tb = traceback.format_exc()
                await send({"type": "lifespan.startup.failed", "message": tb})
                raise  # Let outer MW (Sentry) see + re-raise
            await send({"type": "lifespan.startup.complete"})

            msg = await receive()
            if msg.get("type") != "lifespan.shutdown":
                return
            try:
                await app_self._async_run_shutdown_handlers()
                await app_self._async_run_lifespan_shutdown()
            except BaseException:  # noqa: BLE001
                tb = traceback.format_exc()
                await send({"type": "lifespan.shutdown.failed", "message": tb})
                raise
            await send({"type": "lifespan.shutdown.complete"})

        # Compose the raw ASGI MW chain (outer-most first per add_middleware LIFO)
        composed = _inner_app
        for mw_cls, kwargs in reversed(app_self._raw_asgi_middlewares):
            try:
                composed = mw_cls(app=composed, **kwargs)
            except TypeError:
                composed = mw_cls(**kwargs)

        async def _kickoff():
            state["recv_q"] = asyncio.Queue()
            state["send_done"] = asyncio.Event()

            async def _recv():
                return await state["recv_q"].get()

            async def _send(msg):
                state["send_events"].append(msg)
                t = msg.get("type", "")
                if t.endswith(".complete") or t.endswith(".failed"):
                    state["send_done"].set()

            scope = {
                "type": "lifespan",
                "asgi": {"version": "3.0", "spec_version": "2.0"},
                "state": {},
            }
            state["task"] = asyncio.ensure_future(composed(scope, _recv, _send))
            # Swallow the eventual exception from the re-raised startup/shutdown
            # failure so asyncio doesn't log "Task exception was never retrieved".
            # The error was already observed by the outer MW chain + surfaced
            # via the ``.failed`` ASGI message.
            state["task"].add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            await state["recv_q"].put({"type": "lifespan.startup"})
            await state["send_done"].wait()
            state["send_done"].clear()

            last = state["send_events"][-1] if state["send_events"] else None
            if last and last.get("type") == "lifespan.startup.failed":
                raise RuntimeError(
                    f"Lifespan startup failed: {last.get('message')}"
                )

        _submit(_kickoff(), app=app_self)
        app_self._lifespan_mw_state = state
        return True

    def _stop_lifespan_mw_chain(self) -> bool:
        """Drive ``lifespan.shutdown`` through the raw-ASGI middleware
        chain. Returns True when the chain ran (caller should NOT
        also call ``_run_shutdown_handlers`` directly).

        Lifespan-shutdown FAILURES propagate. Earlier the inner
        send-events queue recorded ``lifespan.shutdown.failed`` but
        ``await state["task"]`` swallowed the exception silently
        — supervisors and TestClient ``__exit__`` saw True (chain
        ran) and never knew cleanup blew up. R39 inspects the last
        queued event AND the task's own exception state; either
        signal escalates to a ``RuntimeError`` with the original
        message."""
        state = getattr(self, "_lifespan_mw_state", None)
        if not state or not state.get("task"):
            return False
        from fastapi_turbo._async_worker import submit as _submit

        outcome: dict = {}

        async def _kickoff():
            state["send_events"].clear()
            await state["recv_q"].put({"type": "lifespan.shutdown"})
            await state["send_done"].wait()
            last = state["send_events"][-1] if state["send_events"] else None
            if last and last.get("type") == "lifespan.shutdown.failed":
                outcome["failed_msg"] = last.get("message", "")
            try:
                await state["task"]
            except BaseException as exc:  # noqa: BLE001
                outcome.setdefault("task_exc", exc)

        _submit(_kickoff(), app=self)
        self._lifespan_mw_state = None
        if "task_exc" in outcome:
            raise outcome["task_exc"]
        if "failed_msg" in outcome:
            raise RuntimeError(
                f"Lifespan shutdown failed: {outcome['failed_msg']}"
            )
        return True

    # ------------------------------------------------------------------
    # Server launch
    # ------------------------------------------------------------------

    def _install_in_process_dynamic_routes(self) -> None:
        """Register the dynamic OpenAPI / docs routes that ``run()``
        normally adds before handing routes to the Rust core, AND
        fire the lifespan ``startup`` events. Used by the
        ``tests/conftest.py`` sandbox-fallback ``server_app``
        fixture (and any other in-process driver that wants the
        OpenAPI/docs surface) so ``GET /openapi.json`` works
        without binding a port.

        Idempotent — repeated calls are no-ops thanks to the
        existing route-deduplication logic in the original ``run()``
        and the ``_lifespan_started`` guard. Lifespan ``shutdown`` is
        registered with ``atexit`` exactly as ``run()`` does."""
        if getattr(self, "_in_process_dynamic_routes_installed", False):
            return
        # Lifespan / startup handlers — same path as ``run()``. We
        # PROPAGATE exceptions here: a startup hook that raises is a
        # real bug, and the FastAPI / Starlette TestClient contract
        # is that startup failures abort the test (not silently turn
        # broken state into passing assertions). Only ``atexit``
        # registration is wrapped — that's pure side-effect
        # bookkeeping and not part of the user-observable startup
        # contract.
        if self._collect_lifespans():
            self._run_lifespan_startup()
            try:
                import atexit
                atexit.register(self._run_lifespan_shutdown)
            except Exception:  # noqa: BLE001
                pass
        self._run_startup_handlers()

        # OpenAPI route — same shape as ``run()`` registers.
        _openapi_url_val = self.openapi_url
        from fastapi_turbo.routing import APIRoute

        # FA contract: ``openapi_url=""`` (empty string) disables
        # the OpenAPI schema endpoint entirely — same as
        # ``openapi_url=None``. Probe-confirmed against
        # ``test_conditional_openapi/test_tutorial001::test_disable
        # _openapi`` which sets the env var to empty string and
        # expects 404.
        if _openapi_url_val:
            _app_ref = self

            def _openapi_dynamic():
                _app_ref.openapi_schema = None
                from fastapi_turbo.responses import JSONResponse as _JR
                return _JR(content=_app_ref.openapi())

            _openapi_dynamic.__name__ = "openapi"

            def _is_prior_dynamic(r, ep_name, path_val):
                ep = getattr(r, "endpoint", None)
                return (
                    ep is not None
                    and getattr(ep, "__name__", None) == ep_name
                    and getattr(r, "path", None) == path_val
                )

            self.router.routes = [
                r for r in self.router.routes
                if not _is_prior_dynamic(r, "openapi", _openapi_url_val)
            ]
            _route = APIRoute(
                _openapi_url_val,
                _openapi_dynamic,
                methods=["GET"],
                include_in_schema=False,
            )
            _route._fastapi_turbo_bypass_deps = True
            self.router.routes.insert(0, _route)

        # Swagger UI / ReDoc HTML routes — Rust path bakes these
        # into ``run_server``; for the in-process path we register
        # Python handlers that return the HTML produced by the
        # ``fastapi.openapi.docs`` helpers.
        if self.docs_url is not None and _openapi_url_val:
            try:
                import fastapi_turbo.compat as _c
                _c.install()
                import sys as _sys
                _docs_mod = _sys.modules.get("fastapi.openapi.docs")
            except Exception:  # noqa: BLE001
                _docs_mod = None
            if _docs_mod is not None and hasattr(_docs_mod, "get_swagger_ui_html"):
                _app_ref2 = self

                def _swagger_dynamic():
                    return _docs_mod.get_swagger_ui_html(
                        openapi_url=_app_ref2.openapi_url,
                        title=_app_ref2.title + " - Swagger UI",
                        oauth2_redirect_url=_app_ref2.swagger_ui_oauth2_redirect_url,
                        init_oauth=_app_ref2.swagger_ui_init_oauth,
                        swagger_ui_parameters=_app_ref2.swagger_ui_parameters,
                    )

                _swagger_dynamic.__name__ = "swagger_ui"
                self.router.routes = [
                    r for r in self.router.routes
                    if not _is_prior_dynamic(r, "swagger_ui", self.docs_url)
                ]
                _swag_route = APIRoute(
                    self.docs_url,
                    _swagger_dynamic,
                    methods=["GET"],
                    include_in_schema=False,
                )
                _swag_route._fastapi_turbo_bypass_deps = True
                self.router.routes.insert(0, _swag_route)

        if self.redoc_url is not None and _openapi_url_val:
            try:
                import fastapi_turbo.compat as _c
                _c.install()
                import sys as _sys
                _docs_mod = _sys.modules.get("fastapi.openapi.docs")
            except Exception:  # noqa: BLE001
                _docs_mod = None
            if _docs_mod is not None and hasattr(_docs_mod, "get_redoc_html"):
                _app_ref3 = self

                def _redoc_dynamic():
                    return _docs_mod.get_redoc_html(
                        openapi_url=_app_ref3.openapi_url,
                        title=_app_ref3.title + " - ReDoc",
                    )

                _redoc_dynamic.__name__ = "redoc"
                self.router.routes = [
                    r for r in self.router.routes
                    if not _is_prior_dynamic(r, "redoc", self.redoc_url)
                ]
                _redoc_route = APIRoute(
                    self.redoc_url,
                    _redoc_dynamic,
                    methods=["GET"],
                    include_in_schema=False,
                )
                _redoc_route._fastapi_turbo_bypass_deps = True
                self.router.routes.insert(0, _redoc_route)

        # Swagger UI's OAuth2 redirect target — upstream FastAPI
        # auto-registers this when ``swagger_ui_oauth2_redirect_url``
        # is set (default ``/docs/oauth2-redirect``). Earlier the
        # in-process installer skipped it, so the upstream
        # ``test_swagger_ui_oauth2_redirect`` test (and any other
        # parity surface that hits this URL) returned 404 in
        # sandboxed / ASGITransport runs. R39 adds the same
        # auto-registration the Rust ``run_server`` path gets.
        if (
            self.swagger_ui_oauth2_redirect_url is not None
            and self.docs_url is not None
            and _openapi_url_val
        ):
            try:
                import fastapi_turbo.compat as _c
                _c.install()
                import sys as _sys
                _docs_mod = _sys.modules.get("fastapi.openapi.docs")
            except Exception:  # noqa: BLE001
                _docs_mod = None
            if _docs_mod is not None and hasattr(
                _docs_mod, "get_swagger_ui_oauth2_redirect_html"
            ):
                def _oauth2_redirect_dynamic():
                    return _docs_mod.get_swagger_ui_oauth2_redirect_html()

                _oauth2_redirect_dynamic.__name__ = "swagger_ui_redirect"
                self.router.routes = [
                    r for r in self.router.routes
                    if not _is_prior_dynamic(
                        r, "swagger_ui_redirect",
                        self.swagger_ui_oauth2_redirect_url,
                    )
                ]
                _oauth2_route = APIRoute(
                    self.swagger_ui_oauth2_redirect_url,
                    _oauth2_redirect_dynamic,
                    methods=["GET"],
                    include_in_schema=False,
                )
                _oauth2_route._fastapi_turbo_bypass_deps = True
                self.router.routes.insert(0, _oauth2_route)

        self._in_process_dynamic_routes_installed = True

    def run(self, host: str = "127.0.0.1", port: int = 8000, **kwargs: Any) -> None:
        """Collect routes, hand them to the Rust core, and start serving."""
        from fastapi_turbo._fastapi_turbo_core import ParamInfo, RouteInfo, run_server

        # Soft DoS-footgun warning: a public-bind (0.0.0.0 / all-zeros
        # IPv6) with no body-size cap means a single client can stream
        # an arbitrary-sized body to OOM the worker. Suppressable via
        # ``FASTAPI_TURBO_SUPPRESS_DOS_WARNING=1`` for users who front
        # the app with an L7 proxy that caps bodies.
        _public_bind = host in ("0.0.0.0", "::", "")
        _no_body_cap = getattr(self, "max_request_size", None) in (None, 0)
        if (
            _public_bind
            and _no_body_cap
            and not os.environ.get("FASTAPI_TURBO_SUPPRESS_DOS_WARNING")
        ):
            import warnings as _w
            _w.warn(
                "fastapi-turbo: binding to a public address without "
                "``FastAPI(max_request_size=...)`` lets a client stream "
                "arbitrarily large bodies to the worker. Either set a "
                "cap (e.g. 10 * 1024 * 1024) or terminate behind a "
                "proxy that enforces one. Set "
                "FASTAPI_TURBO_SUPPRESS_DOS_WARNING=1 to silence.",
                stacklevel=2,
            )

        # Prefer the ASGI-middleware-chained path when raw ASGI middleware
        # is registered — that way Sentry/OTel-style MW that hooks
        # ``scope['type'] == 'lifespan'`` sees startup/shutdown events.
        # The chained path runs both ``_async_run_lifespan_*`` and
        # ``_async_run_*_handlers`` inside a single ``lifespan`` dispatch
        # composed through ``self._raw_asgi_middlewares``.
        if self._start_lifespan_mw_chain():
            atexit.register(self._stop_lifespan_mw_chain)
        else:
            # Direct-call path (no raw ASGI MW to route through).
            if self._collect_lifespans():
                self._run_lifespan_startup()
                atexit.register(self._run_lifespan_shutdown)
            self._run_startup_handlers()
            if self._collect_shutdown_handlers():
                atexit.register(self._run_shutdown_handlers)

        # Register ``/openapi.json`` as a Python handler BEFORE route
        # collection so ``run_server`` hands it to Rust. The handler
        # regenerates the schema per-request, so changes to
        # ``app.root_path`` / ``app.servers`` between TestClient
        # instances surface immediately
        # (``test_openapi_cache_root_path``).
        _openapi_url_val = self.openapi_url
        if _openapi_url_val:
            _app_ref = self

            def _openapi_dynamic():
                _app_ref.openapi_schema = None
                from fastapi_turbo.responses import JSONResponse as _JR
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
            _openapi_route._fastapi_turbo_bypass_deps = True
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
            except Exception as _exc:  # noqa: BLE001
                _log.debug("silent catch in applications: %r", _exc)
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
        if _openapi_url_val:
            openapi_json = None
        _openapi_url_for_rust = self.openapi_url

        middleware_config = self._build_middleware_config()

        # Collect static file mounts for Rust-side ServeDir
        static_mounts = []
        for mount_path, mounted_app, _name in self._mounts:
            if hasattr(mounted_app, 'directory') and mounted_app.directory:
                static_mounts.append((mount_path, str(mounted_app.directory)))

        # Build a not_found_handler callable the Rust 404 fallback can
        # invoke. Signature: ``(method, path, query, headers)`` →
        # ``(status, body_bytes, extra_response_headers)``.
        #
        # Three modes, tried in order:
        #   1) User registered ``@app.exception_handler(404)`` or
        #      ``(HTTPException)`` — dispatch to that handler.
        #   2) ``_http_middlewares`` is non-empty — run the middleware
        #      chain around a synthetic 404 handler so Sentry's
        #      SentryAsgiMiddleware / SessionMiddleware / CORS / etc.
        #      observe the 404 request end-to-end. This matches stock
        #      Starlette's behavior where the Router's default 404
        #      handler runs inside the full MW stack.
        #   3) Nothing to do — let Rust emit the default JSON body.
        not_found_handler = None
        from fastapi_turbo.exceptions import HTTPException as _HTTPExc
        _app_self = self

        def _build_404_request(method, path, query, headers):
            from fastapi_turbo.requests import Request
            # Normalize headers to list[(bytes, bytes)] for ASGI scope.
            hdr_list = []
            for k, v in headers or []:
                if isinstance(k, str):
                    k = k.encode("latin-1")
                if isinstance(v, str):
                    v = v.encode("latin-1")
                hdr_list.append((k, v))
            qs = query if isinstance(query, bytes) else (query or "").encode()
            return Request({
                "type": "http",
                "method": method,
                "path": path,
                "headers": hdr_list,
                "query_string": qs,
                "root_path": getattr(_app_self, "root_path", "") or "",
                "app": _app_self,
                "path_params": {},
            })

        def _extract_response(result):
            """Return (status, body_bytes, [(k, v), ...]) from a Response."""
            import json as _json
            status = getattr(result, "status_code", 404)
            body = getattr(result, "body", None)
            if body is None:
                body = _json.dumps({"detail": "Not Found"}).encode()
            elif isinstance(body, str):
                body = body.encode("utf-8")
            out_headers = []
            raw = getattr(result, "raw_headers", None)
            if raw:
                for k, v in raw:
                    ks = k.decode("latin-1") if isinstance(k, bytes) else k
                    vs = v.decode("latin-1") if isinstance(v, bytes) else v
                    out_headers.append((ks, vs))
            else:
                hdr = getattr(result, "headers", None)
                if hdr is not None:
                    try:
                        for k, v in hdr.items():
                            out_headers.append((str(k), str(v)))
                    except AttributeError:
                        pass
            return (int(status), bytes(body), out_headers)

        def _dispatch_404_via_handler(method, path, query, headers):
            handler = _app_self.exception_handlers.get(404)
            if handler is None:
                handler = _app_self.exception_handlers.get(_HTTPExc)
            if handler is None:
                return None
            req = _build_404_request(method, path, query, headers)
            exc = _HTTPExc(status_code=404, detail="Not Found")
            result = handler(req, exc)
            if inspect.iscoroutine(result):
                import asyncio as _asyncio
                loop = _asyncio.new_event_loop()
                try:
                    result = loop.run_until_complete(result)
                finally:
                    loop.close()
            return _extract_response(result)

        def _dispatch_404_via_middleware(method, path, query, headers):
            """Run the ASGI middleware chain around a synthetic 404
            response so SentryAsgiMiddleware / SessionMiddleware / CORS
            observe the request and can emit tracing / headers."""
            if not _app_self._http_middlewares:
                return None
            try:
                from fastapi_turbo.responses import JSONResponse as _JR
            except ImportError:
                return None

            async def _synthetic_404_handler(request, call_next=None):
                return _JR(content={"detail": "Not Found"}, status_code=404)

            # Build the same chain _wrap_with_http_middlewares does but
            # with our synthetic handler as the innermost call.
            middlewares = list(reversed(_app_self._http_middlewares))

            req = _build_404_request(method, path, query, headers)

            async def _run_chain_async(idx):
                if idx >= len(middlewares):
                    return await _synthetic_404_handler(req)
                mw = middlewares[idx]

                async def call_next(_req=None):
                    return await _run_chain_async(idx + 1)

                if inspect.iscoroutinefunction(mw) or inspect.iscoroutinefunction(
                    getattr(mw, "__call__", None)
                ):
                    return await mw(req, call_next)
                return mw(req, call_next)

            import asyncio as _asyncio
            loop = _asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(_run_chain_async(0))
            finally:
                loop.close()
            if result is None:
                return None
            return _extract_response(result)

        def _rust_404_handler(method, path, query=b"", headers=None):
            # Decode bytes-typed args that Rust passes through.
            if isinstance(method, bytes):
                method = method.decode("latin-1")
            if isinstance(path, bytes):
                path = path.decode("latin-1")
            if isinstance(query, bytes):
                query = query.decode("latin-1")
            # Set the request scope so exception_handlers see the real
            # path even if they introspect ``request.url.path``.
            try:
                _set_current_request_scope(method, path, query)
            except Exception as _exc:  # noqa: BLE001
                _log.debug("silent catch in applications: %r", _exc)
            out = _dispatch_404_via_handler(method, path, query, headers)
            if out is not None:
                return out
            out = _dispatch_404_via_middleware(method, path, query, headers)
            if out is not None:
                return out
            return (404, b'{"detail":"Not Found"}', [])

        if (
            self.exception_handlers.get(404) is not None
            or self.exception_handlers.get(_HTTPExc) is not None
            or self._http_middlewares
        ):
            not_found_handler = _rust_404_handler

        # Rust-side validation dispatcher: when the user registered
        # @exception_handler(RequestValidationError), let the Rust validation
        # error paths route the detail through it.
        validation_handler = None
        from fastapi_turbo.exceptions import RequestValidationError as _RVE
        if _RVE in self.exception_handlers:
            from fastapi_turbo.requests import Request as _Req
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
                import fastapi_turbo.compat as _c
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
            except Exception as _exc:  # noqa: BLE001
                _log.debug("silent catch in applications: %r", _exc)
                swagger_ui_html_str = None
        if self.redoc_url is not None and self.openapi_url is not None:
            try:
                import fastapi_turbo.compat as _c
                _c.install()
                import sys
                _docs_mod = sys.modules.get("fastapi.openapi.docs")
                if _docs_mod is not None:
                    resp = _docs_mod.get_redoc_html(
                        openapi_url=self.openapi_url,
                        title=self.title + " - ReDoc",
                    )
                    redoc_html_str = resp.body.decode("utf-8") if hasattr(resp, "body") else None
            except Exception as _exc:  # noqa: BLE001
                _log.debug("silent catch in applications: %r", _exc)
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

        Dispatch rules:
          * ``lifespan``   → drive startup/shutdown handlers directly.
          * ``http``       → TRY in-process dispatch (match path, run the
                             route's Python endpoint, serialize the
                             Response to ``send`` messages). Falls back
                             to a loopback-proxy path only when the
                             request needs a feature the in-process
                             path doesn't cover (mounts, raw-ASGI
                             middleware, etc.) or an unexpected error
                             bubbles up. This makes the app usable in
                             socket-restricted / sandboxed environments
                             (``httpx.ASGITransport(app=app)``, hermetic
                             test runners) without binding a real port.
          * ``websocket``  → proxy via a loopback server (``websockets``
                             library). In-process WebSocket dispatch is
                             tracked separately.
        """
        if scope["type"] == "lifespan":
            await self._asgi_lifespan(scope, receive, send)
            return

        # Inject the app's configured ``root_path`` into the ASGI
        # scope when the transport didn't already supply one
        # (httpx ``ASGITransport`` and TestClient default to
        # ``""``). FA's reverse-proxy tutorial expects
        # ``request.scope["root_path"]`` to reflect
        # ``FastAPI(root_path="/api/v1")``.
        if scope.get("type") in ("http", "websocket"):
            _app_root = getattr(self, "root_path", "") or ""
            if _app_root and not scope.get("root_path"):
                scope = dict(scope)
                scope["root_path"] = _app_root

        if scope["type"] == "http":
            # Install the dynamic OpenAPI / docs / redoc routes on
            # first ASGI request so ``GET /openapi.json``, ``/docs``,
            # ``/redoc`` work under ``httpx.ASGITransport`` /
            # ``TestClient(app, in_process=True)`` without binding a
            # port. ``run()`` registers these for the Rust server
            # path; the in-process path used to skip them entirely
            # (probe-confirmed: ``/openapi.json`` returned 404 via
            # ``ASGITransport``, breaking ~1273 upstream FastAPI
            # tests in the offline gate). Idempotent — guarded by
            # ``_in_process_dynamic_routes_installed``.
            # Refuse traffic when startup previously failed —
            # ``_run_startup_handlers`` raises with the captured
            # original error. The ASGI transport surfaces the
            # raise to the caller / TestClient. Earlier impl marked
            # the install as "done" on first failure, so subsequent
            # requests slipped past the install-once guard and
            # served 200 against a poisoned app. Probe-confirmed:
            # /ok #2 returned ``{"ok":true,"calls":1}`` after #1
            # raised. Now /ok #2 raises the same RuntimeError as
            # #1 (with the original exception in the message), so
            # the contract is "a failed app stays failed for its
            # lifetime, no traffic served".
            if getattr(self, "_startup_state", "not_started") == "failed":
                self._run_startup_handlers()  # raises

            if not getattr(self, "_in_process_dynamic_routes_installed", False):
                self._install_in_process_dynamic_routes()
            # Honour an explicit opt-out for the (rare) cases where the
            # caller wants the proxy path (existing regression workflows,
            # tests that specifically validate the proxy code path).
            force_proxy = bool(scope.get("_fastapi_turbo_force_proxy"))
            if not force_proxy:
                dispatched = await self._asgi_dispatch_in_process(scope, receive, send)
                if dispatched:
                    return
            # In-process couldn't handle it (or was disabled) — fall
            # back to the loopback Rust server.
            await self._asgi_ensure_server()
            await self._asgi_proxy_http(scope, receive, send)
            return

        if scope["type"] == "websocket":
            # Try in-process WS dispatch first (sandbox-friendly),
            # fall back to the loopback proxy when the in-process
            # path can't satisfy the request.
            force_proxy = bool(scope.get("_fastapi_turbo_force_proxy"))
            if not force_proxy:
                dispatched = await self._asgi_dispatch_ws_in_process(
                    scope, receive, send
                )
                if dispatched:
                    return
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
                except Exception as exc:
                    # Surface the failure to the ASGI server (matches
                    # Starlette / upstream FastAPI). Earlier impl
                    # caught everything and reported
                    # ``lifespan.shutdown.complete`` — production
                    # supervisors lost the failure signal AND the
                    # ``_run_shutdown_handlers`` reset never ran, so
                    # ``_startup_state`` stayed at ``"started"`` and
                    # a reused app skipped startup on the next
                    # cycle (R37 audit caught this).
                    #
                    # Even when shutdown fails, we MUST still reset
                    # the startup guard so a re-used app can
                    # re-start cleanly — otherwise a one-off
                    # cleanup error compounds into a poisoned
                    # second cycle. Reset state here (the early
                    # exception aborted ``_run_shutdown_handlers``
                    # before its tail-side reset).
                    self._startup_state = "not_started"
                    self._startup_failure = None
                    self._in_process_dynamic_routes_installed = False
                    await send({
                        "type": "lifespan.shutdown.failed",
                        "message": str(exc),
                    })
                    return
                await send({"type": "lifespan.shutdown.complete"})
                return

    # ── in-process ASGI dispatch ──────────────────────────────────────
    # Helper lives at module scope below (``_send_asgi_response``) so
    # it can be unit-tested without an app instance.

    async def _asgi_dispatch_in_process(
        self, scope: dict, receive: Callable, send: Callable
    ) -> bool:
        """Route an ASGI ``http`` scope through the Python handler
        pipeline without needing a loopback socket.

        Returns ``True`` when the response was delivered (``send``
        emitted ``http.response.start`` + ``http.response.body``);
        ``False`` when we deliberately gave up so the caller can fall
        back to the proxy path (e.g. request has form/file parts, uses
        a mounted sub-app, or raises a feature we haven't implemented).

        Scope: covers the dispatch surface that matters for ASGI test
        clients and serverless adapters — path + method match, query
        params, JSON body, basic Query/Path/Header params, Pydantic
        body validation, ``Request`` / ``Response`` / ``BackgroundTasks``
        injection, and Starlette-style Response serialization.
        """
        import re as _re
        from fastapi_turbo.requests import Request as _Req
        from fastapi_turbo.responses import (
            JSONResponse as _JR,
            Response as _Resp,
        )
        from fastapi_turbo.encoders import jsonable_encoder as _je
        from fastapi_turbo.exceptions import (
            HTTPException as _HE,
            RequestValidationError as _RVE,
        )

        method = scope.get("method", "GET").upper()
        path = scope.get("path", "/")

        # ── Host dispatch ──
        # ``app.host("subapp", subapp)`` — when a request's Host header
        # matches a registered host, recurse into the sub-app's
        # ``__call__``. Done HERE (before mount dispatch and route
        # match) because the host middleware installed via
        # ``app.host()`` only fires inside an endpoint's wrapper
        # chain — when no main-app route matches, the dispatcher
        # would return False and fall back to the loopback Rust
        # server, which can't bind under ``ASGITransport`` /
        # serverless / sandbox runs (probe-confirmed: bare
        # ``httpx.ASGITransport(app=app)`` raised
        # ``PermissionError`` instead of routing to the sub-app).
        hosts = getattr(self, "_hosts", None)
        if hosts:
            host_header = ""
            for hk, hv in scope.get("headers", []) or []:
                hkl = (hk.decode("latin-1") if isinstance(hk, bytes) else hk).lower()
                if hkl == "host":
                    host_header = (
                        hv.decode("latin-1") if isinstance(hv, bytes) else hv
                    )
                    break
            hs = host_header.split(":", 1)[0].lower()
            for entry in hosts:
                hn = entry[0].lower()
                sub = entry[1]
                if sub is None:
                    continue
                hit = (hn == hs) or ("." not in hn and hs.split(".", 1)[0] == hn)
                if hit:
                    # Forward the (unchanged) scope to the sub-app's
                    # ASGI ``__call__``. Sub-app keeps its own route
                    # table; the path on the scope is whatever the
                    # client sent (Starlette's ``Host`` doesn't strip
                    # a prefix — that's what ``mount`` is for).
                    await sub(scope, receive, send)
                    return True

        # ── Mount dispatch ──
        # ``app.mount("/v1", subapp)`` — if the incoming path starts
        # with a registered mount prefix AND no top-level route matches
        # the path verbatim, recurse into the sub-app with the prefix
        # stripped. Top-level literal routes win over mount dispatch
        # (a user-registered ``/status`` beats a mount at ``/`` that
        # would also match).
        for mount_path, mounted_app, _mname in getattr(self, "_mounts", []) or []:
            prefix = mount_path.rstrip("/")
            if not prefix:
                # Mount at root ``/`` — only match when no top-level
                # route exists for this path; defer that check to the
                # normal matcher below so root mounts fall through
                # naturally.
                continue
            if path == prefix or path.startswith(prefix + "/"):
                # Don't shadow a top-level literal route. Only redirect
                # to the sub-app when NO top-level route matches.
                top_level_hit = False
                for r in self.router.routes:
                    if getattr(r, "path", None) == path and method in (
                        {m.upper() for m in (getattr(r, "methods", None) or ())}
                    ):
                        top_level_hit = True
                        break
                if not top_level_hit:
                    sub_path = path[len(prefix):] or "/"
                    sub_scope = dict(scope)
                    sub_scope["path"] = sub_path
                    sub_scope["raw_path"] = sub_path.encode("latin-1")
                    sub_scope["root_path"] = scope.get("root_path", "") + prefix
                    # Sub-apps implement ASGI via __call__; delegate.
                    if callable(mounted_app):
                        await mounted_app(sub_scope, receive, send)
                        return True
                    # APIRouter mount: ``app.mount('/v2', router)`` where
                    # ``router`` is a bare APIRouter (no ``__call__``).
                    # Build a transient FastAPI app, ``include_router``
                    # the user's router, and delegate to it once.
                    from fastapi_turbo.routing import APIRouter as _APIRouter
                    if isinstance(mounted_app, _APIRouter):
                        sub_app = type(self)()
                        try:
                            sub_app.include_router(mounted_app)
                        except Exception as _exc:  # noqa: BLE001
                            _log.debug(
                                "in-process APIRouter mount: %r", _exc
                            )
                        await sub_app(sub_scope, receive, send)
                        return True

        if not hasattr(self, "router") or not getattr(self.router, "routes", None):
            return False

        # Pre-assemble a Starlette-like request scope for downstream
        # consumers (the user's endpoint may inject ``Request``).
        # Enforce ``FastAPI(max_request_size=…)`` for the in-process
        # path so TestClient / ASGITransport fallback rejects
        # oversized bodies the same way the Tower layer does on the
        # Rust server. Without this a request that would 413 in
        # production would silently succeed in fallback tests.
        _max_body = getattr(self, "max_request_size", None)

        class _BodyTooLarge(Exception):
            pass

        async def _drain_body() -> bytes:
            body = b""
            while True:
                msg = await receive()
                body += msg.get("body", b"")
                if _max_body is not None and _max_body > 0 and len(body) > _max_body:
                    raise _BodyTooLarge()
                if not msg.get("more_body", False):
                    break
            return body

        # ── Raw-ASGI middleware chain ──
        # If the app has registered raw-ASGI middlewares via
        # ``add_middleware(MWClass, ...)``, we wrap our in-process
        # endpoint dispatch in a leaf ASGI app and compose the MW
        # chain around it (LIFO per add_middleware order — matches
        # Starlette / FastAPI semantics). This lets Sentry / CORS /
        # GZip / TrustedHost / Session / user-custom ASGI MWs observe
        # and mutate the request without requiring loopback access.
        #
        # Compose the in-process middleware chain in REGISTRATION
        # order, spanning both Tower-bound markers
        # (CORS/GZip/HTTPSRedirect) and raw ASGI middlewares. Without
        # this, Tower markers would always be outermost regardless of
        # ``add_middleware`` order — breaking the case where a user
        # adds a custom ASGI middleware AFTER ``HTTPSRedirectMiddleware``
        # to decorate the redirect response.
        #
        # Tower marker classes are inert as ASGI on their own; we
        # substitute the real Starlette implementation around the
        # shim. Raw ASGI classes are user-provided so we use them
        # as-is.
        log = list(getattr(self, "_mw_registration_log", None) or [])
        raw_mws: list[tuple[type, dict[str, Any]]] = []
        for kind, mw_cls, mw_kwargs, _seq in log:
            if kind == "tower":
                resolved = _resolve_tower_bound_to_asgi_class(mw_cls)
                if resolved is not None:
                    raw_mws.append((resolved, mw_kwargs))
            else:
                raw_mws.append((mw_cls, mw_kwargs))
        # Fallback for apps registered before ``_mw_registration_log``
        # was introduced (e.g. internal compat shims that bypass
        # ``add_middleware``): merge what's in the legacy lists.
        if not log:
            for mw_cls, mw_kwargs in (
                getattr(self, "_raw_asgi_middlewares", None) or []
            ):
                raw_mws.append((mw_cls, mw_kwargs))
            for mw_cls, mw_kwargs in (
                getattr(self, "_middleware_stack", None) or []
            ):
                resolved = _resolve_tower_bound_to_asgi_class(mw_cls)
                if resolved is not None:
                    raw_mws.append((resolved, mw_kwargs))
        if raw_mws:
            # Build a leaf ASGI app that re-enters ``_asgi_dispatch_in_process``
            # with a flag signalling we've already applied the MW chain.
            flagged_scope = dict(scope)
            flagged_scope["_fastapi_turbo_mw_applied"] = True

            async def _leaf(inner_scope, inner_receive, inner_send):
                dispatched = await self._asgi_dispatch_in_process(
                    inner_scope, inner_receive, inner_send
                )
                if not dispatched:
                    # No route matched the inner-stripped scope; emit
                    # a 404 via ASGI so the MW chain can observe the
                    # response shape.
                    await _send_asgi_response(
                        inner_send,
                        _JR(content={"detail": "Not Found"}, status_code=404),
                    )

            # Iterate forward (NOT reversed): ``add_middleware(X)``
            # then ``add_middleware(Y)`` means Y is the outermost.
            # forward-wrap gives ``Y(X(leaf))`` → Y.__call__ runs first.
            composed = _leaf
            for mw_cls, mw_kwargs in raw_mws:
                try:
                    composed = mw_cls(app=composed, **mw_kwargs)
                except TypeError:
                    composed = mw_cls(**mw_kwargs)
            if not scope.get("_fastapi_turbo_mw_applied"):
                await composed(flagged_scope, receive, send)
                return True
            # Else fall through — we're inside the chain recursion
            # and should dispatch the actual endpoint.

        # Two-phase route match: first find a method-matching route;
        # if none, collect the set of methods declared for the same
        # path so we can emit 405 + Allow (FA parity) instead of
        # falling through to 404 or the proxy.
        matched_route = None
        path_params: dict = {}
        # ``methods_for_path`` holds the methods of the FIRST route
        # whose path matches (regardless of method). Starlette's
        # matcher reports only the first-matching route's methods
        # in the 405 Allow header — accumulating across all routes
        # diverges from upstream when the user registers
        # ``@app.get("/r")`` and ``@app.post("/r")`` as two separate
        # routes (which produces two distinct route objects in
        # Starlette and ``Allow: GET`` for a PUT to /r).
        methods_for_path: set[str] = set()
        for route in self.router.routes:
            r_path = getattr(route, "path", None)
            if not r_path:
                continue
            regex = getattr(route, "_fastapi_turbo_asgi_regex", None)
            if regex is None:
                pattern = "^"
                idx = 0
                for m in _re.finditer(r"\{([^{}:]+)(?::([^{}]+))?\}", r_path):
                    pattern += _re.escape(r_path[idx:m.start()])
                    pname = m.group(1)
                    if m.group(2) == "path":
                        pattern += f"(?P<{pname}>.+)"
                    else:
                        pattern += f"(?P<{pname}>[^/]+)"
                    idx = m.end()
                pattern += _re.escape(r_path[idx:]) + "$"
                regex = _re.compile(pattern)
                try:
                    route._fastapi_turbo_asgi_regex = regex  # type: ignore[attr-defined]
                except (AttributeError, TypeError):
                    pass
            match = regex.match(path)
            if match is None:
                continue
            r_methods = {m.upper() for m in (getattr(route, "methods", None) or ())}
            # Only the FIRST matching route contributes to the 405
            # Allow header — matches Starlette's first-hit semantics.
            if not methods_for_path:
                methods_for_path = r_methods
            if method in r_methods:
                matched_route = route
                path_params = match.groupdict()
                break

        if matched_route is None:
            # Trailing-slash redirect (Starlette ``redirect_slashes=True``
            # default). Only fires when NO route matches the path for
            # ANY method — i.e. ``methods_for_path`` is empty. If some
            # method matches the path, fall through to 405 so we don't
            # bounce OPTIONS / unsupported methods through a redirect
            # loop on ``{full_path:path}`` style routes (which would
            # match both ``/files/x`` AND ``/files/x/``, ping-ponging
            # forever).
            if (
                getattr(self, "redirect_slashes", True)
                and path != "/"
                and not methods_for_path
            ):
                if path.endswith("/"):
                    candidate = path[:-1]
                else:
                    candidate = path + "/"
                for route in self.router.routes:
                    r_path = getattr(route, "path", None)
                    if not r_path:
                        continue
                    regex = getattr(
                        route, "_fastapi_turbo_asgi_regex", None
                    )
                    if regex is None:
                        # Lazy compile mirrors the matcher block above.
                        pattern = "^"
                        idx = 0
                        for m in _re.finditer(
                            r"\{([^{}:]+)(?::([^{}]+))?\}", r_path
                        ):
                            pattern += _re.escape(r_path[idx:m.start()])
                            pname = m.group(1)
                            if m.group(2) == "path":
                                pattern += f"(?P<{pname}>.+)"
                            else:
                                pattern += f"(?P<{pname}>[^/]+)"
                            idx = m.end()
                        pattern += _re.escape(r_path[idx:]) + "$"
                        regex = _re.compile(pattern)
                        try:
                            route._fastapi_turbo_asgi_regex = regex
                        except (AttributeError, TypeError):
                            pass
                    if regex.match(candidate) is not None:
                        # Build an ABSOLUTE Location URL — Starlette's
                        # redirect_slashes middleware constructs the
                        # full request.url with the path swapped, so
                        # downstream HTTP clients that require an
                        # absolute Location see the same shape as
                        # upstream. Reconstructed from scope's scheme
                        # + host header.
                        qs = scope.get("query_string", b"")
                        scheme = scope.get("scheme", "http")
                        host = ""
                        for hk, hv in scope.get("headers", []) or []:
                            hkn = (
                                hk.decode("latin-1")
                                if isinstance(hk, bytes)
                                else hk
                            ).lower()
                            if hkn == "host":
                                host = (
                                    hv.decode("latin-1")
                                    if isinstance(hv, bytes)
                                    else hv
                                )
                                break
                        if not host:
                            server = scope.get("server")
                            if server:
                                host = (
                                    f"{server[0]}:{server[1]}"
                                    if server[1]
                                    else server[0]
                                )
                        target = candidate
                        if qs:
                            qs_str = (
                                qs.decode("latin-1")
                                if isinstance(qs, bytes)
                                else str(qs)
                            )
                            target = f"{target}?{qs_str}"
                        if host:
                            target = f"{scheme}://{host}{target}"
                        await send({
                            "type": "http.response.start",
                            "status": 307,
                            "headers": [
                                (b"location", target.encode("latin-1")),
                                (b"content-length", b"0"),
                            ],
                        })
                        await send({
                            "type": "http.response.body",
                            "body": b"",
                        })
                        return True

            # Path matched but method didn't → 405 with Allow; else 404.
            # Emitted IN-PROCESS so sandboxed envs don't fall through to
            # the loopback proxy for normal FA behaviour.
            if methods_for_path:
                from fastapi_turbo.responses import JSONResponse as _JR_err
                allow = ", ".join(sorted(methods_for_path))
                resp = _JR_err(
                    content={"detail": "Method Not Allowed"}, status_code=405
                )
                resp.headers["allow"] = allow
                await _send_asgi_response(send, resp)
            else:
                # Build a 404 via the app's exception_handlers so custom
                # handlers fire — fall back to {"detail": "Not Found"} 404.
                from fastapi_turbo.exceptions import HTTPException as _HE404
                await _asgi_emit_exception(
                    self, scope, send, _HE404(status_code=404, detail="Not Found")
                )
            return True

        # Confirm we can handle every param on this endpoint. If the
        # signature uses a feature we don't cover (Form, File, Depends,
        # OAuth scopes, …) bail NOW — before draining the body — so the
        # proxy can serve it.
        #
        # Custom APIRoute subclass with overridden ``get_route_handler``
        # (e.g. ``GzipRoute``, ``TimedRoute``, projects that wrap every
        # response with auth/CSRF/observability headers). The wrapper
        # is the user's API for intercepting the request pipeline; if
        # we silently unwrap to the bare endpoint we'd skip it and
        # break drop-in parity with FastAPI.
        #
        # ``_build_custom_route_handler_endpoint`` wires up
        # ``_fastapi_turbo_build_default_handler`` on the route (as a
        # side effect) AND returns a ``(request) -> response`` adapter
        # that:
        #   1. Calls ``route.get_route_handler()`` — invokes user's
        #      override (e.g. WrappedRoute), which can call
        #      ``super().get_route_handler()`` to get the default
        #      pipeline and wrap its result.
        #   2. Drives the resulting handler with the Request.
        #   3. Routes HTTPException / RequestValidationError through
        #      the app's exception_handlers.
        # Idempotent — safe to call once per request; the lazy build
        # is cheap and the resulting endpoint isn't cached on the
        # route (only the builder attribute is, which is what
        # ``super().get_route_handler()`` needs).
        if _has_overridden_get_route_handler(matched_route):
            try:
                custom_endpoint = _build_custom_route_handler_endpoint(
                    matched_route, self
                )
            except Exception as _exc:  # noqa: BLE001
                _log.debug("custom route handler build failed: %r", _exc)
            else:
                # Build a Starlette-shaped Request scope inline (the
                # main dispatcher builds one further down, but we
                # need it here BEFORE the unwrap branch). Pre-drain
                # the receive channel and stash it so
                # ``request.body()`` inside the user's wrapper or
                # endpoint reads from the buffer rather than
                # re-receiving.
                custom_req_scope = dict(scope)
                custom_req_scope["type"] = "http"
                custom_req_scope["path_params"] = path_params
                custom_req_scope["app"] = self
                custom_req_scope["route"] = matched_route
                buffered_body = b""
                while True:
                    msg = await receive()
                    buffered_body += msg.get("body", b"")
                    if not msg.get("more_body", False):
                        break
                custom_req_scope["_fastapi_turbo_prebuffered_body"] = buffered_body
                custom_req_scope["_body"] = buffered_body
                # Wrap the route-class endpoint in the SAME
                # ``@app.middleware('http')`` chain the regular path
                # uses. Without this, ``X-App-Mw`` headers added by
                # ``app.middleware('http')`` would be missing on
                # routes registered through a custom ``route_class``.
                http_mws_for_custom = [
                    m
                    for m in (getattr(self, "_http_middlewares", None) or [])
                    if not getattr(m, "_fastapi_turbo_is_asgi_shim", False)
                ]
                current_call = custom_endpoint
                for mw in http_mws_for_custom:
                    _inner = current_call

                    async def _wrapped_custom(req, *, _mw=mw, _inner=_inner):
                        return await _mw(req, _inner)

                    current_call = _wrapped_custom
                req_obj = _Req(custom_req_scope)
                try:
                    result = await current_call(req_obj)
                except Exception as exc:
                    await _asgi_emit_exception(self, scope, send, exc)
                    return True
                await _send_asgi_response(send, result, scope=scope)
                return True

        # The route's ``endpoint`` is the ``_try_compile_handler``
        # wrapper that expects Rust-synthesised kwargs
        # (``_combined_body``, ``_request_*`` metadata, etc.). The
        # in-process path builds user-shape kwargs (``a=A(...)``), so
        # we dispatch to the UNWRAPPED user function via the
        # ``_fastapi_turbo_original_endpoint`` breadcrumb set at
        # compile time.
        raw_endpoint = getattr(matched_route, "endpoint", None)
        if raw_endpoint is None:
            return False
        endpoint = getattr(raw_endpoint, "_fastapi_turbo_original_endpoint", raw_endpoint)
        import inspect as _insp
        try:
            sig = _insp.signature(endpoint)
        except (TypeError, ValueError):
            return False

        # ── Build the param plan via ``_introspect.introspect_endpoint`` ──
        # Same introspection the Rust hot path uses, so we get Pydantic-
        # full semantics (ge/le constraints, list[T] aggregation, alias
        # + convert_underscores, Annotated[T, marker] unwrapping,
        # Body(embed=True), multi-body combination, scalar_validator
        # TypeAdapters) without maintaining a second resolver.
        from fastapi_turbo._introspect import introspect_endpoint
        from fastapi_turbo.responses import Response as _Resp_cls
        from fastapi_turbo.background import BackgroundTasks as _BGT
        from fastapi_turbo.requests import HTTPConnection as _HC

        _plan_cache_attr = "_fastapi_turbo_asgi_param_plan"
        introspect_params = getattr(matched_route, _plan_cache_attr, None)
        if introspect_params is None:
            try:
                introspect_params = introspect_endpoint(
                    endpoint, getattr(matched_route, "path", "/") or "/"
                )
            except Exception as _exc:  # noqa: BLE001
                _log.debug("introspect_endpoint failed: %r", _exc)
                return False
            try:
                setattr(matched_route, _plan_cache_attr, introspect_params)
            except (AttributeError, TypeError):
                pass

        # Pre-scan: does the endpoint need a body? (so we know whether
        # to drain receive.)
        survey_needs_body = any(
            p.get("kind") in ("body", "form", "file") for p in introspect_params
        )

        # Build a FastAPI-ish Request scope.
        req_scope = dict(scope)
        req_scope["type"] = "http"
        req_scope["path_params"] = path_params
        req_scope["app"] = self
        req_scope["route"] = matched_route
        # Also mutate the OUTER scope so middleware that wraps the
        # ASGI app (legacy ``SentryAsgiMiddleware(app)``, OTel,
        # rate-limit) sees the matched route at response-time.
        # Sentry's transaction name uses ``scope["route"].path`` to
        # template the URL — without this it records the concrete
        # path (``/message/123456``) instead of the route shape
        # (``/message/{message_id}``). Starlette's router does the
        # same in-place mutation; we previously only updated the
        # copied ``req_scope`` so the outer scope stayed empty.
        try:
            scope["route"] = matched_route
            scope["path_params"] = path_params
            scope["endpoint"] = getattr(matched_route, "endpoint", None)
        except (TypeError, AttributeError):
            # Some upstream wrappers hand us a frozen / mapping-only
            # scope; in that case the in-place mutation isn't possible
            # and the legacy-Sentry shape stays untemplated. Not an
            # error — most middleware uses ``scope`` as a plain dict.
            pass
        # Sentry's ``FastApiIntegration`` patches
        # ``fastapi.routing.get_request_handler`` to set the
        # transaction name from ``scope["route"].path`` on every
        # request. Our dispatcher doesn't go through that handler,
        # so the patch never fires — Sentry falls back to the
        # concrete-URL transaction name and ``test_legacy_setup``
        # diffs ``http://testserver:None/message/123456`` against
        # the expected ``/message/{message_id}``. Replicate the
        # transaction-name update inline when Sentry's
        # ``FastApiIntegration`` is loaded; cheap import probe
        # (cached after the first run), no-op when Sentry isn't
        # installed.
        _maybe_set_sentry_transaction_name(self, scope, matched_route)
        try:
            body_bytes = await _drain_body()
        except _BodyTooLarge:
            # Mirror the Tower layer's 413 with a short text body —
            # Starlette / FastAPI surface oversized requests with a
            # plain text response and no JSON envelope.
            _msg = b"Request Entity Too Large"
            await send({
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(_msg)).encode("ascii")),
                ],
            })
            await send({
                "type": "http.response.body",
                "body": _msg,
            })
            return True
        except Exception as _drain_exc:  # noqa: BLE001
            # A user-supplied raw-ASGI middleware can wrap ``receive``
            # to raise ``HTTPException`` (e.g. content-size limits).
            # Route through the standard exception emit path —
            # without this the exception escaped past the dispatcher
            # and the test client saw a network-level failure rather
            # than the user's intended 422. Probe-confirmed against
            # ``test_custom_middleware_exception``.
            await _asgi_emit_exception(self, scope, send, _drain_exc)
            return True
        req_scope["_fastapi_turbo_prebuffered_body"] = body_bytes
        req_scope["_body"] = body_bytes

        # ── Parse form / multipart body if any ``Form(...)`` or
        # ``File(...)`` params are present on the endpoint. Done once
        # up-front so subsequent kwarg assembly is O(1) per param.
        # Also fire the parser when a ``Depends(...)`` callable has
        # form params nested inside its own ``__init__`` (e.g.
        # ``OAuth2PasswordRequestFormStrict``) — checking only the
        # outer plan would miss those and the dep resolver later sees
        # an empty form_fields, raising spurious 422 missing errors.
        form_fields: dict[str, object] = {}
        _outer_has_form = any(
            p.get("kind") in ("form", "file") for p in introspect_params
        )
        _form_ct_match = False
        if not _outer_has_form and body_bytes:
            for _hk, _hv in scope.get("headers", []) or []:
                _hkl = (
                    _hk.decode("latin-1") if isinstance(_hk, bytes) else _hk
                ).lower()
                if _hkl == "content-type":
                    _hvl = (
                        _hv.decode("latin-1") if isinstance(_hv, bytes) else _hv
                    ).lower()
                    if (
                        _hvl.startswith("application/x-www-form-urlencoded")
                        or _hvl.startswith("multipart/form-data")
                    ):
                        # Only parse if a Depends(...) anywhere on the
                        # route has at least one form param — content-
                        # type alone isn't enough (a JSON endpoint with
                        # a wrong CT shouldn't waste cycles parsing).
                        for _pp in introspect_params:
                            if _pp.get("kind") != "dependency":
                                continue
                            _dc = _pp.get("dep_callable")
                            if _dc is None:
                                continue
                            try:
                                _dplan_probe = introspect_endpoint(_dc, "/")
                            except Exception:  # noqa: BLE001
                                continue
                            if any(
                                _dpp.get("kind") in ("form", "file")
                                for _dpp in _dplan_probe
                            ):
                                _form_ct_match = True
                                break
                    break
        if (_outer_has_form or _form_ct_match) and body_bytes:
            import io
            content_type = ""
            for hk, hv in scope.get("headers", []) or []:
                hkl = (hk.decode("latin-1") if isinstance(hk, bytes) else hk).lower()
                if hkl == "content-type":
                    content_type = hv.decode("latin-1") if isinstance(hv, bytes) else hv
                    break
            ct_lower = content_type.lower()
            try:
                if ct_lower.startswith("application/x-www-form-urlencoded"):
                    from urllib.parse import parse_qsl
                    for k, v in parse_qsl(body_bytes.decode("utf-8"), keep_blank_values=True):
                        # Repeated keys ⇒ list (matches Starlette's
                        # ``FormData`` semantics). Without this a
                        # ``list[str]`` form param only saw the LAST
                        # value of a multi-value submission.
                        _existing_uf = form_fields.get(k)
                        if _existing_uf is None:
                            form_fields[k] = v
                        elif isinstance(_existing_uf, list):
                            _existing_uf.append(v)
                        else:
                            form_fields[k] = [_existing_uf, v]
                elif ct_lower.startswith("multipart/form-data"):
                    import email.parser as _email_parser
                    # RFC 2045 §5.1: param names case-insensitive,
                    # values case-sensitive — ``Boundary=AaB03x`` is
                    # valid. Lowercase the lookup key, preserve value.
                    boundary = None
                    for part in content_type.split(";"):
                        part = part.strip()
                        if "=" in part:
                            k, v = part.split("=", 1)
                            if k.strip().lower() == "boundary":
                                boundary = v.strip().strip('"')
                                break
                    if boundary is not None:
                        from fastapi_turbo.param_functions import UploadFile as _UF
                        # Starlette's MultiPartParser defaults — exceeding
                        # either bounds the request to a 400, defending the
                        # endpoint against unbounded form expansion.
                        _max_files = 1000
                        _max_fields = 1000
                        _files_seen = 0
                        _fields_seen = 0
                        raw = (
                            f"Content-Type: multipart/form-data; boundary={boundary}\r\n\r\n"
                        ).encode("utf-8") + body_bytes
                        msg = _email_parser.BytesParser().parsebytes(raw)
                        for part_msg in msg.walk():
                            if part_msg.is_multipart():
                                continue
                            cd = part_msg.get("content-disposition", "")
                            if not cd:
                                continue
                            # Param NAMES case-insensitive (RFC 2045
                            # §5.1) — lowercase keys, preserve values.
                            # ``Content-Disposition: form-data;
                            # Name="x"; FileName="y.txt"`` parses to
                            # the same fields as the canonical case.
                            params: dict[str, str] = {}
                            for seg in cd.split(";"):
                                seg = seg.strip()
                                if "=" in seg:
                                    k, v = seg.split("=", 1)
                                    params[k.strip().lower()] = v.strip().strip('"')
                            fname = params.get("name")
                            if fname is None:
                                continue
                            if "filename" in params:
                                _files_seen += 1
                                if _files_seen > _max_files:
                                    from fastapi_turbo.exceptions import (
                                        MultiPartException as _MPE,
                                    )
                                    raise _MPE(
                                        "Too many files. Maximum number of "
                                        f"files is {_max_files}."
                                    )
                                payload = part_msg.get_payload(decode=True) or b""
                                _new_uf = _UF(
                                    filename=params["filename"],
                                    file=io.BytesIO(payload),
                                    content_type=part_msg.get_content_type(),
                                    # Match Starlette's MultiPartParser:
                                    # initialise ``size`` so subsequent
                                    # ``await file.write(b)`` increments
                                    # it (otherwise size stays None and
                                    # ``UploadFile.size`` reports None
                                    # forever — diverges from upstream).
                                    size=len(payload),
                                )
                                _existing = form_fields.get(fname)
                                if _existing is None:
                                    form_fields[fname] = _new_uf
                                elif isinstance(_existing, list):
                                    _existing.append(_new_uf)
                                else:
                                    form_fields[fname] = [_existing, _new_uf]
                            else:
                                _fields_seen += 1
                                if _fields_seen > _max_fields:
                                    from fastapi_turbo.exceptions import (
                                        MultiPartException as _MPE,
                                    )
                                    raise _MPE(
                                        "Too many fields. Maximum number of "
                                        f"fields is {_max_fields}."
                                    )
                                val = part_msg.get_payload(decode=True) or b""
                                if isinstance(val, bytes):
                                    val = val.decode("utf-8", errors="replace")
                                _existing = form_fields.get(fname)
                                if _existing is None:
                                    form_fields[fname] = val
                                elif isinstance(_existing, list):
                                    _existing.append(val)
                                else:
                                    form_fields[fname] = [_existing, val]
            except Exception as _exc:  # noqa: BLE001
                # Body has already been drained; we cannot fall back to
                # the proxy (it would see an empty body). Surface the
                # error in-process so the client gets a FA-shaped 422
                # and the server doesn't silently re-run the handler
                # against a stripped payload.
                _log.debug("in-process form parse: %r", _exc)
                from fastapi_turbo.exceptions import (
                    MultiPartException as _MPE_check,
                    RequestValidationError as _RVE_form,
                )
                if isinstance(_exc, _MPE_check):
                    # Starlette/FastAPI map MultiPartException to a
                    # plain 400 with ``{"detail": <msg>}`` — not the
                    # 422 RequestValidationError envelope. Match that
                    # so over-limit uploads get the same response.
                    import json as _j
                    _payload = _j.dumps({"detail": _exc.message}).encode("utf-8")
                    await send({
                        "type": "http.response.start",
                        "status": 400,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(_payload)).encode("ascii")),
                        ],
                    })
                    await send({"type": "http.response.body", "body": _payload})
                    return True
                await _asgi_emit_exception(
                    self, scope, send,
                    _RVE_form([{
                        "type": "form_parse_error",
                        "loc": ["body"],
                        "msg": f"form parse failed: {_exc}",
                        "input": None,
                    }]),
                )
                return True

        # ── Shared helpers used by dep resolution + kwarg assembly ──
        from fastapi_turbo.exceptions import RequestValidationError as _RVE_err

        def _missing(loc, pname):
            return {
                "type": "missing",
                "loc": [loc, pname],
                "msg": "Field required",
                "input": None,
            }

        def _bad_type(loc, pname, expected, raw):
            return {
                "type": f"{expected}_parsing",
                "loc": [loc, pname],
                "msg": f"Input should be a valid {expected}",
                "input": raw,
            }

        # ``_PARAM_MODEL_MISSING`` is the sentinel
        # ``_maybe_expand_param_models`` puts in ``default_value`` for
        # synthesized list-shaped extraction params. The builder dep
        # later checks ``is _PARAM_MODEL_MISSING`` to decide whether
        # the field was supplied — passing it through unchanged keeps
        # that contract. For ``Optional[list[X]] = None`` the user's
        # actual default is ``None`` and they want ``None`` back; only
        # fall through to ``[]`` when there is no explicit default
        # (which currently can't happen on a non-required list, but
        # the third branch is defensive).
        from fastapi_turbo._introspect import (
            _PARAM_MODEL_MISSING as _PMM_local,
        )

        def _list_default_for_missing(_pdesc, dv, has_dflt):
            if dv is _PMM_local:
                return _PMM_local
            if has_dflt:
                if isinstance(dv, (list, tuple, set, frozenset)):
                    return list(dv)
                return dv
            return []

        def _coerce(raw, target_ann):
            if target_ann is int:
                return int(raw)
            if target_ann is float:
                return float(raw)
            if target_ann is bool:
                if isinstance(raw, bool):
                    return raw
                rs = str(raw).lower()
                if rs in ("true", "1", "yes", "on"):
                    return True
                if rs in ("false", "0", "no", "off"):
                    return False
                raise ValueError(f"not a bool: {raw!r}")
            return raw

        def _is_required_default(default):
            """True when a ``Query(...) / Header(...) / Cookie(...)``
            marker signals "required". Pydantic v2 uses
            ``PydanticUndefined`` as the sentinel; older FA also
            treated ``...`` as required; we accept both."""
            if default is ...:
                return True
            # Pydantic v2 sentinel — avoid importing at module load.
            _type_name = type(default).__name__
            return _type_name == "PydanticUndefinedType"

        # Parse query string once so deps + params share the same view.
        qp: dict = {}
        _qs_bytes = scope.get("query_string", b"")
        if _qs_bytes:
            from urllib.parse import parse_qsl
            qp = dict(parse_qsl(_qs_bytes.decode("latin-1")))

        # Parse request headers/cookies once so deps can read them.
        from fastapi_turbo.datastructures import Headers as _Hdrs_cls
        _scope_headers = _Hdrs_cls(scope.get("headers", []))
        _scope_cookies: dict[str, str] = {}
        for _ck_name, _ck_val in [
            (k, v) for k, v in _scope_headers.items() if k.lower() == "cookie"
        ]:
            for pair in _ck_val.split(";"):
                pair = pair.strip()
                if "=" in pair:
                    ck_k, ck_v = pair.split("=", 1)
                    _scope_cookies[ck_k.strip()] = ck_v.strip()

        # ── Depends / Security resolution ──
        # Resolve any ``= Depends(fn)`` or ``= Security(fn, scopes=...)``
        # params (including nested deps, async deps, yield-deps,
        # dependency_overrides) before we build kwargs. Teardowns are
        # collected and run after the response. ``Security`` scopes
        # accumulate along the resolution path so an inner dep that
        # asks for ``ss: SecurityScopes`` sees the full scope list.
        from fastapi_turbo.dependencies import Depends as _Dep_marker
        from fastapi_turbo.dependencies import Security as _Sec_marker
        from fastapi_turbo.security import SecurityScopes as _SS_cls

        dep_teardowns: list = []  # (gen, is_async) pairs

        # Lazily-allocated single ``Response`` instance shared between
        # the handler and any deps that ask for a ``response: Response``
        # parameter. FastAPI's contract: deps mutating ``response.headers``
        # must affect the FINAL response the user sees (e.g. an auth
        # dep that adds an ``X-Auth-Source`` header to every reply).
        # The handler-level resolver below allocates this on first
        # use; the dep resolver pulls from the same slot.
        _shared_response_holder: list = [None]
        # Same shared-state pattern for ``BackgroundTasks`` so a dep
        # that calls ``bg.add_task(...)`` and the handler that does
        # the same end up scheduling against ONE container.
        _bg_holder: list = [None]

        def _get_or_create_response_inject():
            if _shared_response_holder[0] is None:
                _shared_response_holder[0] = _Resp_cls()
            return _shared_response_holder[0]

        async def _resolve_dep(marker_or_fn, cache, accumulated_scopes, dep_scope=None, use_cache=True, _own_scopes_for_key=None):
            """Resolve ``marker_or_fn`` recursively. ``cache`` dedups
            calls for the same dep within one request (FA caching).
            ``accumulated_scopes`` is the list of OAuth2 scopes
            collected along the resolution path (consumed by any
            ``SecurityScopes`` param deeper in the chain).
            """
            # FA 0.120+ ``scope`` carries through Depends/Security
            # markers — capture before unwrapping so the inner
            # generator's teardown is queued on the right list. Same
            # for ``use_cache``. The Security marker's OWN
            # ``scopes`` argument also matters for the cache key
            # (FA's ``own_oauth_scopes`` triggers ``_uses_scopes``
            # → cache by scope list).
            _marker_scope = None
            _marker_use_cache = use_cache
            _marker_own_scopes: list = []
            if isinstance(marker_or_fn, (_Sec_marker, _Dep_marker)):
                _marker_scope = getattr(marker_or_fn, "scope", None)
                _marker_use_cache = getattr(marker_or_fn, "use_cache", True)
                if isinstance(marker_or_fn, _Sec_marker):
                    _marker_own_scopes = list(
                        getattr(marker_or_fn, "scopes", []) or []
                    )
            # Unwrap Security marker → extend scopes + resolve its callable.
            if isinstance(marker_or_fn, _Sec_marker):
                next_scopes = list(accumulated_scopes) + list(
                    getattr(marker_or_fn, "scopes", []) or []
                )
                dep_fn = marker_or_fn.dependency
                if dep_fn is None:
                    return None
                return await _resolve_dep(
                    dep_fn, cache, next_scopes,
                    dep_scope=_marker_scope, use_cache=_marker_use_cache,
                    _own_scopes_for_key=_marker_own_scopes,
                )
            if isinstance(marker_or_fn, _Dep_marker):
                dep_fn = marker_or_fn.dependency
                if dep_fn is None:
                    return None
                return await _resolve_dep(
                    dep_fn, cache, accumulated_scopes,
                    dep_scope=_marker_scope, use_cache=_marker_use_cache,
                )
            dep_fn = marker_or_fn
            # Honour dependency_overrides.
            override = (getattr(self, "dependency_overrides", None) or {}).get(dep_fn)
            actual_fn = override if override is not None else dep_fn
            # FA 0.120+: ``Depends(dep, scope='function')`` and
            # ``Depends(dep, scope='request')`` resolve to DIFFERENT
            # instances within one request (the function-scope copy
            # is torn down pre-response while request-scope persists
            # past it). The cache key includes ``dep_scope`` to keep
            # them separate. ``accumulated_scopes`` participate ONLY
            # when the dep callable's signature actually consumes
            # ``SecurityScopes`` — otherwise the same callable
            # caches once per request regardless of which Security
            # scope chain reached it. Probe-confirmed against
            # ``test_security_scopes_dependency_called_once`` (no
            # SecurityScopes → 1 call) and
            # ``test_security_scopes_sub_dependency_caching`` (uses
            # SecurityScopes → distinct calls per scope chain).
            # Walk the dep callable's signature transitively; if it
            # OR any nested dep consumes ``SecurityScopes``, the
            # cache key includes the accumulated scope tuple.
            # Otherwise the dep caches by callable alone — same dep
            # called from two scope chains hits the same entry.
            _dep_uses_security_scopes = False
            _seen_for_scope_check: set = set()

            def _consumes_security_scopes(_fn):
                if _fn in _seen_for_scope_check:
                    return False
                _seen_for_scope_check.add(_fn)
                try:
                    _s = _insp.signature(_fn)
                except (TypeError, ValueError):
                    return False
                from fastapi_turbo.dependencies import (
                    Depends as _D2,
                    Security as _S2,
                )
                for _pp in _s.parameters.values():
                    _ann = _pp.annotation
                    if isinstance(_ann, type) and issubclass(_ann, _SS_cls):
                        return True
                    # Walk into nested Depends/Security default markers.
                    _dft = _pp.default
                    if isinstance(_dft, (_D2, _S2)) and _dft.dependency is not None:
                        if _consumes_security_scopes(_dft.dependency):
                            return True
                    # Walk into ``Annotated[T, Depends(...)]`` markers.
                    if hasattr(_ann, "__metadata__"):
                        for _m in getattr(_ann, "__metadata__", ()):
                            if isinstance(_m, (_D2, _S2)) and _m.dependency is not None:
                                if _consumes_security_scopes(_m.dependency):
                                    return True
                return False
            try:
                _dep_uses_security_scopes = _consumes_security_scopes(actual_fn)
            except Exception:  # noqa: BLE001
                _dep_uses_security_scopes = False
            # FA's ``_uses_scopes`` is also True when THIS
            # Dependant's own_oauth_scopes is non-empty — i.e. the
            # IMMEDIATE Security marker had ``scopes=[...]``.
            if _marker_own_scopes or _own_scopes_for_key:
                _dep_uses_security_scopes = True
            if _dep_uses_security_scopes:
                cache_key = (
                    actual_fn,
                    tuple(sorted(accumulated_scopes)),
                    (dep_scope or "request").lower(),
                )
            else:
                cache_key = (
                    actual_fn,
                    (dep_scope or "request").lower(),
                )
            # When the caller asked for ``use_cache=False``, skip the
            # cache lookup AND don't write back. FA's contract: each
            # ``Depends(d, use_cache=False)`` runs the dep fresh.
            _do_cache = use_cache
            if _do_cache and cache_key in cache:
                return cache[cache_key]
            try:
                sub_sig = _insp.signature(actual_fn)
            except (TypeError, ValueError):
                sub_sig = None
            sub_hints = {}
            try:
                sub_hints = _tp.get_type_hints(actual_fn)
            except Exception:  # noqa: BLE001
                pass
            # First try to use the SAME introspect plan for the dep
            # that we use for the top-level endpoint — this covers
            # ``Annotated[int, Query(ge=10, alias=...)]`` and every
            # other Pydantic-constraint case correctly.
            try:
                _dep_plan = introspect_endpoint(actual_fn, "/")
            except Exception:  # noqa: BLE001
                _dep_plan = None
            sub_kwargs = {}
            # Accumulate per-dep validation errors so a dep callable
            # whose ``__init__`` declares 3 required ``Form(...)`` params
            # (e.g. ``OAuth2PasswordRequestFormStrict``) emits all 3
            # ``missing`` entries in one 422 — matches FA. Without
            # this we raised on the first missing and the user only
            # saw one of the three.
            _form_missing_errs: list = []
            if _dep_plan is not None and sub_sig is not None:
                for dp in _dep_plan:
                    dname = dp["name"]
                    dkind = dp.get("kind")
                    dalias = dp.get("alias") or dname
                    drequired = dp.get("required", False)
                    ddefault = dp.get("default_value")
                    dscalar = dp.get("scalar_validator")
                    dann = dp.get("_unwrapped_annotation")
                    if dkind == "dependency":
                        # Nested Depends/Security inside the dep.
                        inner_call = dp.get("dep_callable")
                        if inner_call is None:
                            sub_kwargs[dname] = None
                            continue
                        # Accumulate scopes from any Security markers
                        # attached to this sub-dep (captured in
                        # ``_security_scopes_top``).
                        next_scopes = list(accumulated_scopes) + list(
                            dp.get("_security_scopes_top") or []
                        )
                        sub_kwargs[dname] = await _resolve_dep(
                            inner_call, cache, next_scopes,
                            dep_scope=dp.get("_dep_scope"),
                            use_cache=dp.get("use_cache", True),
                        )
                        continue
                    if dkind == "query":
                        # FA contract: a bare ``param: int`` on a
                        # dep (no Query/Header/etc marker) takes its
                        # value from the route's PATH params if the
                        # name matches a path placeholder. Falls
                        # back to query otherwise. Probe-confirmed
                        # against ``test_param_in_path_and_dependency``.
                        if dalias in path_params:
                            sub_kwargs[dname] = _validate(
                                dscalar, path_params[dalias], "path",
                                dalias, annotation=dann,
                            )
                            continue
                        raw_q = None
                        for qk, qv in _qp_items:
                            if qk == dalias:
                                raw_q = qv
                                break
                        if raw_q is None:
                            if drequired:
                                raise _RVE_err([_missing("query", dalias)])
                            sub_kwargs[dname] = ddefault
                        else:
                            sub_kwargs[dname] = _validate(
                                dscalar, raw_q, "query", dalias, annotation=dann
                            )
                        continue
                    if dkind == "header":
                        raw_h = _scope_headers.get(dalias)
                        if raw_h is None:
                            if drequired:
                                raise _RVE_err([_missing("header", dalias)])
                            sub_kwargs[dname] = ddefault
                        else:
                            sub_kwargs[dname] = _validate(
                                dscalar, raw_h, "header", dalias, annotation=dann
                            )
                        continue
                    if dkind == "cookie":
                        raw_c = _scope_cookies.get(dalias)
                        if raw_c is None:
                            if drequired:
                                raise _RVE_err([_missing("cookie", dalias)])
                            sub_kwargs[dname] = ddefault
                        else:
                            sub_kwargs[dname] = _validate(
                                dscalar, raw_c, "cookie", dalias, annotation=dann
                            )
                        continue
                    if dkind in ("form", "file"):
                        # Form / file params on a dep callable
                        # (typically FA's ``OAuth2PasswordRequestForm``
                        # / ``OAuth2PasswordRequestFormStrict`` whose
                        # ``__init__`` declares ``grant_type`` /
                        # ``username`` / ``password`` / ``scope`` etc.
                        # as ``Form(...)`` markers). Earlier they fell
                        # through to the parameter-default fallback
                        # and required fields raised
                        # ``TypeError: missing keyword-only argument``
                        # when the dep was instantiated.
                        d_is_list = dp.get("container_type") is not None or (
                            _tp_local.get_origin(dann) is list
                        )
                        d_val = form_fields.get(dalias)
                        if d_is_list:
                            if d_val is None:
                                if drequired:
                                    _form_missing_errs.append(
                                        _missing("body", dalias)
                                    )
                                    continue
                                sub_kwargs[dname] = _list_default_for_missing(
                                    dp, ddefault, dp.get("has_default", False)
                                )
                                continue
                            d_vals = d_val if isinstance(d_val, list) else [d_val]
                            if dkind == "file":
                                sub_kwargs[dname] = d_vals
                            else:
                                sub_kwargs[dname] = _validate(
                                    dscalar, d_vals, "body", dalias, annotation=dann
                                )
                            continue
                        if d_val is None:
                            if drequired:
                                _form_missing_errs.append(
                                    _missing("body", dalias)
                                )
                                continue
                            sub_kwargs[dname] = ddefault
                        else:
                            if isinstance(d_val, list):
                                d_val = d_val[0]
                            if dkind == "file":
                                sub_kwargs[dname] = d_val
                            else:
                                sub_kwargs[dname] = _validate(
                                    dscalar, d_val, "body", dalias, annotation=dann
                                )
                        continue
                    # SecurityScopes → inject.
                    raw_ann = dp.get("_raw_annotation")
                    if isinstance(raw_ann, type) and issubclass(raw_ann, _SS_cls):
                        sub_kwargs[dname] = _SS_cls(scopes=list(accumulated_scopes))
                        continue
                    # Response / Request / BackgroundTasks injection
                    # for deps. Earlier the dep resolver only handled
                    # query / header / cookie / dependency / SecurityScopes
                    # — params like ``response: Response`` fell through
                    # to the query fallback and got None. Probe-confirmed
                    # against upstream's
                    # ``test_include_router_defaults_overrides`` (43
                    # tests): a dep mutating ``response.headers``
                    # crashed because response was None. Now we share
                    # one Response instance with the handler-level
                    # resolver via ``_shared_response_holder`` so
                    # mutations propagate to the final response.
                    if dkind == "inject_response":
                        sub_kwargs[dname] = _get_or_create_response_inject()
                        continue
                    if dkind == "inject_request":
                        sub_kwargs[dname] = _Req(req_scope, receive=receive)
                        continue
                    if dkind == "inject_background_tasks":
                        # Reuse the request's BG container so deps and
                        # handler share one ``add_task`` queue.
                        nonlocal_bg = _bg_holder[0]
                        if nonlocal_bg is None:
                            nonlocal_bg = _BGT()
                            nonlocal_bg._app = self
                            _bg_holder[0] = nonlocal_bg
                        sub_kwargs[dname] = nonlocal_bg
                        continue
                    # Fallback: parameter default.
                    if drequired:
                        # Legacy non-marker scalar; no sensible fallback.
                        pass
                    else:
                        sub_kwargs[dname] = ddefault
                # Skip the legacy sub_sig-walk below; the introspect
                # plan has already populated ``sub_kwargs``.
                sub_sig = None
                if _form_missing_errs:
                    raise _RVE_err(_form_missing_errs)
            if sub_sig is not None:
                from fastapi_turbo.param_functions import _ParamMarker as _PM_dep
                for sname, sp in sub_sig.parameters.items():
                    sdefault = sp.default
                    sann = sub_hints.get(sname, sp.annotation)
                    # SecurityScopes injection.
                    if isinstance(sann, type) and issubclass(sann, _SS_cls):
                        sub_kwargs[sname] = _SS_cls(scopes=list(accumulated_scopes))
                        continue
                    if isinstance(sdefault, _Sec_marker):
                        next_scopes = list(accumulated_scopes) + list(
                            getattr(sdefault, "scopes", []) or []
                        )
                        inner = sdefault.dependency or sann
                        sub_kwargs[sname] = await _resolve_dep(
                            inner, cache, next_scopes
                        )
                        continue
                    if isinstance(sdefault, _Dep_marker):
                        inner = sdefault.dependency or sann
                        sub_kwargs[sname] = await _resolve_dep(
                            inner, cache, accumulated_scopes
                        )
                        continue
                    # Query/Header/Cookie markers inside a dep signature.
                    sdefault_marker = None
                    if isinstance(sdefault, _PM_dep):
                        sdefault_marker = sdefault
                    if sdefault_marker is not None:
                        _pm_kind_sub = getattr(sdefault_marker, "_kind", None)
                        alias = getattr(sdefault_marker, "alias", None) or sname
                        raw = None
                        if _pm_kind_sub == "query":
                            raw = qp.get(alias)
                        elif _pm_kind_sub == "header":
                            raw = _scope_headers.get(alias.replace("_", "-"))
                        elif _pm_kind_sub == "cookie":
                            raw = _scope_cookies.get(alias)
                        if raw is None:
                            default = getattr(sdefault_marker, "default", ...)
                            if default is ...:
                                raise _RVE_err([_missing(_pm_kind_sub, alias)])
                            sub_kwargs[sname] = default
                        else:
                            try:
                                sub_kwargs[sname] = _coerce(raw, sann)
                            except (ValueError, TypeError):
                                raise _RVE_err(
                                    [_bad_type(
                                        _pm_kind_sub, alias,
                                        getattr(sann, "__name__", "str"), raw
                                    )]
                                ) from None
                        continue
                    # Fallback: query by name with type coercion /
                    # default value from the parameter itself.
                    if sname in qp:
                        try:
                            sub_kwargs[sname] = _coerce(qp[sname], sann)
                        except (ValueError, TypeError):
                            raise _RVE_err(
                                [_bad_type(
                                    "query", sname,
                                    getattr(sann, "__name__", "str"), qp[sname]
                                )]
                            ) from None
                    elif sdefault is not _insp.Parameter.empty:
                        sub_kwargs[sname] = sdefault
                    elif isinstance(sann, type) and issubclass(sann, (_Req, _HC)):
                        sub_kwargs[sname] = _Req(req_scope)
                    # else: leave unset; the dep may have **kwargs etc.
            # FA 0.120+ scope semantics: ``function`` teardowns run
            # IMMEDIATELY after the handler returns (before the
            # response is sent — they CAN raise to abort the
            # response with a 503). ``request`` (default) teardowns
            # run AFTER the response is flushed.
            _scope_for_gen = (dep_scope or "request").lower()
            if _insp.isasyncgenfunction(actual_fn):
                gen = actual_fn(**sub_kwargs)
                val = await gen.__anext__()
                dep_teardowns.append((gen, True, _scope_for_gen))
            elif _insp.isgeneratorfunction(actual_fn):
                gen = actual_fn(**sub_kwargs)
                val = next(gen)
                dep_teardowns.append((gen, False, _scope_for_gen))
            elif _insp.iscoroutinefunction(actual_fn):
                val = await actual_fn(**sub_kwargs)
            else:
                val = actual_fn(**sub_kwargs)
                # Detect generators / coroutines / async generators
                # produced by callables that don't trip the function-
                # level introspection: class instances whose ``__call__``
                # is async / a generator (FA's ``OAuth2``,
                # ``ClassInstanceGenDep``), or callables wrapped in
                # ``functools.wraps`` (a plain ``def wrapper`` returning
                # the inner generator). Inspect the return value.
                if _insp.iscoroutine(val):
                    val = await val
                elif _insp.isasyncgen(val):
                    gen = val
                    val = await gen.__anext__()
                    dep_teardowns.append((gen, True, _scope_for_gen))
                elif _insp.isgenerator(val):
                    gen = val
                    val = next(gen)
                    dep_teardowns.append((gen, False, _scope_for_gen))
            if _do_cache:
                cache[cache_key] = val
            return val

        import typing as _tp

        kwargs: dict = {}
        response_injected = None  # Track so we can fold its cookies back.
        bg_injected = None
        dep_cache: dict = {}

        # Pre-compute the full query-params multi-dict (preserves
        # repeats for list[T] aggregation).
        from urllib.parse import parse_qsl
        _qp_items: list[tuple[str, str]] = []
        if _qs_bytes:
            _qp_items = parse_qsl(_qs_bytes.decode("latin-1"), keep_blank_values=True)

        def _alias_for_header(marker, pname):
            """Honour ``validation_alias`` / ``alias`` /
            ``convert_underscores`` on Header(...). FA precedence:
            ``validation_alias`` wins over ``alias``, both win over
            the dash-converted python name."""
            va = getattr(marker, "validation_alias", None)
            if isinstance(va, str) and va:
                return va
            if getattr(marker, "alias", None):
                return marker.alias
            convert = getattr(marker, "convert_underscores", True)
            return pname.replace("_", "-") if convert else pname

        def _extract_list_from_query(alias):
            return [v for k, v in _qp_items if k == alias]

        # Cache auto-built TypeAdapters for primitive annotations so
        # we don't re-construct one per request for `int` / `float` /
        # `bool` params that ``introspect`` left with
        # ``scalar_validator = None``.
        _auto_adapters: dict = {}

        def _get_adapter(adapter, annotation):
            """Pick the adapter to use: ``adapter`` if present, else
            build one on the fly for primitive annotations that need
            string→T coercion (int / float / bool / etc.)."""
            if adapter is not None:
                return adapter
            if annotation in _auto_adapters:
                return _auto_adapters[annotation]
            if annotation in (None, inspect.Parameter.empty, _insp.Parameter.empty):
                return None
            try:
                from pydantic import TypeAdapter as _TA
                built = _TA(annotation)
            except Exception:  # noqa: BLE001
                built = None
            _auto_adapters[annotation] = built
            return built

        def _validate(adapter, raw, loc_kind, loc_name, annotation=None):
            """Run a Pydantic TypeAdapter against a raw value. Errors
            become FA-shaped 422 RequestValidationErrors. When
            ``adapter`` is None and ``annotation`` is a primitive, we
            auto-build an adapter so ``?n=42`` coerces to int."""
            eff = _get_adapter(adapter, annotation)
            if eff is None:
                return raw
            from pydantic import ValidationError as _PyVE
            try:
                return eff.validate_python(raw)
            except _PyVE as pve:
                errs = []
                for e in pve.errors():
                    # Strip ``url`` (pydantic doc-link, not in upstream
                    # FastAPI's response shape) but PRESERVE ``ctx``
                    # (constraint values like ``{min_length: 1}``) —
                    # upstream FastAPI emits ctx in its 422 response
                    # bodies. R39 audit caught us silently dropping
                    # it across ~888 upstream tests.
                    new = {k: v for k, v in e.items() if k != "url"}
                    loc = list(new.get("loc", ()))
                    new["loc"] = [loc_kind, loc_name, *loc] if loc else [loc_kind, loc_name]
                    errs.append(new)
                raise _RVE_err(errs) from None

        try:
            import typing as _tp_local
            from fastapi_turbo.dependencies import Depends as _Dep_marker2

            # Collect every Body()-kind param so we can support
            # multi-body (implicit embed) and explicit embed=True.
            body_params = [p for p in introspect_params if p.get("kind") == "body"]
            body_embed_single = (
                len(body_params) == 1
                and body_params[0].get("_embed")
            )
            multi_body = len(body_params) > 1

            # Parse the body once (if any body param exists). Dispatch
            # on the request Content-Type AND the body param's own
            # ``media_type`` (set via ``Body(..., media_type=…)``):
            #   * JSON-shaped bodies (default, or ``application/json``)
            #     → ``json.loads`` → 422 on decode error.
            #   * Body params whose declared media_type is non-JSON
            #     (e.g. ``application/octet-stream``) AND whose
            #     declared annotation is bytes-shaped → pass raw bytes
            #     to the param without JSON parsing.
            # Without this, a binary upload handler that takes
            # ``payload: bytes = Body(..., media_type='application/
            # octet-stream')`` would 422 because the binary payload
            # isn't valid UTF-8 / JSON.
            parsed_body: object = None
            body_parsed = False
            # Pull the request's Content-Type once (lower-cased,
            # parameter-stripped) for dispatch.
            _req_ct = ""
            for hk, hv in scope.get("headers", []) or []:
                if (hk.decode("latin-1") if isinstance(hk, bytes) else hk).lower() == "content-type":
                    _req_ct = (
                        (hv.decode("latin-1") if isinstance(hv, bytes) else hv)
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    break

            def _body_param_wants_raw_bytes(bp) -> bool:
                """A body param wants raw bytes if EITHER the
                declared annotation is ``bytes``/``bytearray`` OR
                the marker carries a non-JSON ``media_type``
                (e.g. ``application/octet-stream``)."""
                ann = bp.get("_unwrapped_annotation")
                if ann is bytes or ann is bytearray:
                    return True
                mt = bp.get("media_type") or ""
                if mt and not mt.lower().startswith("application/json"):
                    return True
                return False

            wants_raw_bytes = (
                len(body_params) == 1
                and _body_param_wants_raw_bytes(body_params[0])
            ) or (
                _req_ct
                and not _req_ct.startswith("application/json")
                and len(body_params) == 1
                and body_params[0].get("_unwrapped_annotation") in (bytes, bytearray)
            )

            # FA 0.120+ ``strict_content_type=True`` (default): the
            # body is rejected with 422 if the request's Content-Type
            # doesn't match the param's declared media type. The Rust
            # path enforces this at compile time; the in-process
            # dispatcher needs the same check. Closest-wins
            # precedence: route → router → app.
            #
            # Strict mode rejects BOTH missing CT and wrong CT; lax
            # mode accepts missing CT but still rejects a present-
            # but-wrong CT (probe-confirmed against
            # ``test_lax_post_with_text_plain_is_still_rejected``).
            _route_strict_eff = getattr(matched_route, "strict_content_type", None)
            if _route_strict_eff is None:
                _router_for_route = getattr(matched_route, "_fastapi_turbo_owner_router", None)
                if _router_for_route is not None:
                    _route_strict_eff = getattr(_router_for_route, "strict_content_type", None)
            if _route_strict_eff is None:
                _route_strict_eff = getattr(self, "strict_content_type", True)
            _strict_active = _route_strict_eff is True
            _lax_active = _route_strict_eff is False
            if (
                (_strict_active or _lax_active)
                and body_params
                and body_bytes
                and not wants_raw_bytes
            ):
                # Determine the expected content-type family from the
                # body params' kinds. Form/File → form/multipart;
                # otherwise JSON. Mixed body+form is illegal — the
                # introspect plan would surface that earlier.
                _has_form_param = any(
                    p.get("kind") in ("form", "file") for p in introspect_params
                )
                if _has_form_param:
                    _ok_cts = (
                        "application/x-www-form-urlencoded",
                        "multipart/form-data",
                    )
                else:
                    _ok_cts = ("application/json",)
                _req_ct_full = ""
                for _hk, _hv in scope.get("headers", []) or []:
                    if (
                        _hk.decode("latin-1")
                        if isinstance(_hk, bytes)
                        else _hk
                    ).lower() == "content-type":
                        _req_ct_full = (
                            _hv.decode("latin-1")
                            if isinstance(_hv, bytes)
                            else _hv
                        ).lower()
                        break
                _ct_type_ok = bool(_req_ct_full) and any(
                    _req_ct_full.startswith(_c)
                    or (_c == "application/json" and "+json" in _req_ct_full)
                    for _c in _ok_cts
                )
                # Strict mode: require Content-Type to be PRESENT.
                # Don't check the type — let body parsing proceed
                # and surface Pydantic's ``model_attributes_type``
                # (or similar) on a wrong CT so the client sees the
                # real type mismatch. Probe-confirmed against
                # ``test_post_form_for_json``.
                # Lax mode: don't require CT presence. But if CT IS
                # present, it must be a JSON-compatible type — text/
                # plain with a JSON-shaped body should still 422
                # because the user EXPLICITLY declared the wrong CT.
                # Probe-confirmed against
                # ``test_lax_post_with_text_plain_is_still_rejected``.
                if _strict_active and not _req_ct_full:
                    raise _RVE_err([{
                        "type": "missing",
                        "loc": ["header", "content-type"],
                        "msg": "Field required",
                        "input": None,
                    }])
                if _lax_active and _req_ct_full and not _ct_type_ok:
                    raise _RVE_err([{
                        "type": "missing",
                        "loc": ["header", "content-type"],
                        "msg": f"Unexpected Content-Type: {_req_ct_full}",
                        "input": _req_ct_full,
                    }])

            if body_params and body_bytes:
                if wants_raw_bytes:
                    parsed_body = bytes(body_bytes)
                    body_parsed = True
                else:
                    import json as _json
                    # Detect the request's content-type to drive the
                    # body-parse strategy. When CT is anything OTHER
                    # than JSON (e.g. text/plain, form-encoded for a
                    # JSON endpoint), DON'T parse JSON — pass the raw
                    # body string to Pydantic so it surfaces a
                    # ``model_attributes_type`` error with the raw
                    # body in ``input``. FA's contract: the user
                    # explicitly set the wrong CT, so the parse step
                    # is skipped; the model validator rejects with a
                    # Pydantic error rather than us silently coercing
                    # JSON-shaped text/plain. Probe-confirmed against
                    # ``test_tutorial/test_body/test_tutorial001::test_
                    # wrong_headers``.
                    _ct_for_parse = ""
                    for _hk_p, _hv_p in scope.get("headers", []) or []:
                        if (
                            _hk_p.decode("latin-1")
                            if isinstance(_hk_p, bytes)
                            else _hk_p
                        ).lower() == "content-type":
                            _ct_for_parse = (
                                _hv_p.decode("latin-1")
                                if isinstance(_hv_p, bytes)
                                else _hv_p
                            ).lower()
                            break
                    _ct_main = _ct_for_parse.split(";", 1)[0].strip()
                    _ct_is_json = (
                        not _ct_for_parse
                        or _ct_main == "application/json"
                        or (
                            _ct_main.startswith("application/")
                            and _ct_main.endswith("+json")
                        )
                    )
                    _has_form_param_for_parse = any(
                        p.get("kind") in ("form", "file")
                        for p in introspect_params
                    )
                    if not _ct_is_json and not _has_form_param_for_parse:
                        # Pass raw body string to body validation —
                        # model_class type-check at line 8830 surfaces
                        # ``model_attributes_type``.
                        try:
                            parsed_body = body_bytes.decode("utf-8")
                        except Exception:  # noqa: BLE001
                            parsed_body = repr(body_bytes)
                        body_parsed = True
                    else:
                        try:
                            parsed_body = _json.loads(body_bytes)
                            body_parsed = True
                        except _json.JSONDecodeError as jde:
                            # FA shape: ``msg`` is the bare ``"JSON
                            # decode error"`` and the position-
                            # specific detail lives in
                            # ``ctx={"error": jde.msg}``.
                            raise _RVE_err([{
                                "type": "json_invalid",
                                "loc": ["body", jde.pos],
                                "msg": "JSON decode error",
                                "input": {},
                                "ctx": {"error": jde.msg},
                            }]) from None
                        except Exception as _other_je:  # noqa: BLE001
                            # FA contract: a non-JSONDecodeError from
                            # ``json.loads`` (e.g. ``json`` patched
                            # out in tests, MemoryError, etc.) → 400
                            # via HTTPException so the test sees a
                            # graceful failure rather than a 500.
                            # Probe-confirmed against
                            # ``test_tutorial/test_body/test_tutorial001
                            # ::test_other_exceptions``.
                            from fastapi_turbo.exceptions import (
                                HTTPException as _HE_je,
                            )
                            raise _HE_je(
                                status_code=400,
                                detail="There was an error parsing the body",
                            ) from _other_je
            elif body_params and not body_bytes:
                # Body expected but empty — emit a missing-body 422
                # matching FA's shape. For an embedded single body
                # (``Annotated[Item, Body(embed=True)]``) the loc is
                # ``["body", "item"]`` — earlier we always emitted
                # ``["body"]`` and dropped the field name. The
                # combined-body path handles its own multi-field
                # missing emission downstream.
                _missing_body_errs: list = []
                for _bp in body_params:
                    if not _bp.get("required"):
                        continue
                    _bp_name = _bp.get("name")
                    if _bp_name == "_combined_body":
                        # Defer to the combined-body branch which
                        # emits one missing per field.
                        continue
                    _bp_alias = _bp.get("alias") or _bp_name
                    _bp_embed = _bp.get("_embed", False)
                    if _bp_embed:
                        _missing_body_errs.append({
                            "type": "missing",
                            "loc": ["body", _bp_alias],
                            "msg": "Field required",
                            "input": None,
                        })
                    else:
                        _missing_body_errs.append({
                            "type": "missing",
                            "loc": ["body"],
                            "msg": "Field required",
                            "input": None,
                        })
                if _missing_body_errs:
                    raise _RVE_err(_missing_body_errs)

            # Run app/router/route-level extra dependencies (those
            # declared via ``FastAPI(dependencies=[...])`` /
            # ``APIRouter(dependencies=[...])`` /
            # ``@app.get(..., dependencies=[...])``) BEFORE the handler
            # params are resolved. Their return values are discarded
            # (matches FA — extra deps run for side effects: auth,
            # metrics, audit logging). Errors propagate so HTTP
            # exceptions / 422s surface correctly.
            #
            # Skips routes flagged ``_fastapi_turbo_bypass_deps`` —
            # the docs / openapi.json endpoints opt out of all
            # user-registered deps so a misconfigured auth dep
            # doesn't lock you out of the schema.
            if not getattr(matched_route, "_fastapi_turbo_bypass_deps", False):
                _extra_dep_markers: list = []
                _extra_dep_markers.extend(
                    getattr(self, "dependencies", []) or []
                )
                _extra_dep_markers.extend(
                    getattr(self.router, "dependencies", []) or []
                )
                # Route-level deps stamped on the shadow clone via
                # ``include_router`` (or directly on the Route).
                _extra_dep_markers.extend(
                    getattr(matched_route, "dependencies", []) or []
                )
                # Include-time deps: the shadow clone may carry
                # ``_fastapi_turbo_include_deps`` from the include_router
                # walker (see routing.py / FastAPI.include_router).
                _extra_dep_markers.extend(
                    getattr(matched_route, "_fastapi_turbo_include_deps", [])
                    or []
                )
                # Accumulate validation errors across multiple extra
                # deps (app-level, router-level, route-level). FA
                # contract: when 2+ extra deps EACH miss a required
                # query/header/cookie/form param, ALL of them surface
                # in one 422 response. Earlier we raised on the first
                # dep's miss and the client only saw 1 of N errors —
                # mismatch with upstream's bigger-applications tutorial
                # snapshots.
                _xdep_accum_errs: list = []
                for _xdep in _extra_dep_markers:
                    try:
                        await _resolve_dep(_xdep, dep_cache, [])
                    except _RVE_err as _rve:
                        _xdep_accum_errs.extend(_rve.errors())
                if _xdep_accum_errs:
                    # Convert tuple ``loc`` (returned by ``.errors()``)
                    # back to list shape that other emit sites use.
                    _xdep_norm = [
                        {**e, "loc": list(e["loc"])}
                        if isinstance(e.get("loc"), tuple)
                        else e
                        for e in _xdep_accum_errs
                    ]
                    raise _RVE_err(_xdep_norm)

            # Accumulate per-endpoint form/file ``missing`` errors so a
            # request body that omits multiple required form fields
            # surfaces ALL of them in one 422 — matches FA. Earlier we
            # raised on the first missing and the client only saw a
            # single entry. Same accumulator covers query / header /
            # cookie / path missing + bad-type so a request that
            # supplies multiple invalid params shows them all in one
            # 422 (FA's contract — ``test_foo_no_needy`` expects 3
            # entries: 1 missing + 2 int_parsing).
            _outer_form_missing_errs: list = []
            for p in introspect_params:
                name = p["name"]
                kind = p.get("kind")
                alias = p.get("alias") or name
                required = p.get("required", False)
                has_default = p.get("has_default", False)
                default_val = p.get("default_value")
                scalar_validator = p.get("scalar_validator")
                model_class = p.get("model_class")
                container_type = p.get("container_type")
                _ann_for_list = p.get("_unwrapped_annotation")
                _ann_origin = _tp_local.get_origin(_ann_for_list)
                # Pydantic ``Json[T]`` (``Annotated[T, pydantic.Json]``)
                # is a JSON-encoded SCALAR — wire value is a single
                # string. Don't trip the multi-value list path even
                # if the inner T is ``list[X]``. Probe-confirmed
                # against ``test_json_type::test_form_json_list`` /
                # ``test_query_json_list`` / ``test_header_json_list``.
                _raw_ann = p.get("_raw_annotation")
                _is_json_marker = False
                try:
                    if _raw_ann is not None and hasattr(_raw_ann, "__metadata__"):
                        for _m in getattr(_raw_ann, "__metadata__", ()):
                            # Pydantic ``Json`` is a class whose
                            # parameterised forms (``Json[list[str]]``)
                            # produce subclasses with the same
                            # ``__name__``. ``m is pydantic.Json``
                            # fails for those; match on class name +
                            # module instead.
                            _mc = _m if isinstance(_m, type) else type(_m)
                            if (
                                getattr(_mc, "__name__", None) == "Json"
                                and getattr(_mc, "__module__", "").startswith("pydantic")
                            ):
                                _is_json_marker = True
                                break
                except Exception:  # noqa: BLE001
                    pass
                is_list_param = (not _is_json_marker) and (
                    container_type is not None
                    or _ann_origin in (list, set, frozenset, tuple)
                    # Bare ``list`` / ``set`` / ``tuple`` annotations
                    # (no parametrization) are still sequence params —
                    # FA collects repeated values into the appropriate
                    # container. ``test_forms_from_non_typing_sequences``
                    # asserts ``items: list = Form()`` returns
                    # ``["first", "second", "third"]`` for a 3-value
                    # form submission.
                    or _ann_for_list in (list, set, frozenset, tuple)
                )

                if kind == "dependency":
                    dep_callable = p.get("dep_callable")
                    if dep_callable is None:
                        kwargs[name] = None
                        continue
                    # FA 0.115+ parameter-model synthetic builder:
                    # ``_maybe_expand_param_models`` flattens
                    # ``p: Annotated[MyModel, Query()]`` into N synthetic
                    # extraction params (one per model field) plus this
                    # builder dep that reconstructs the model from those
                    # extracted values. Its ``dep_input_map`` pre-wires
                    # the builder's kwargs to the synthesized field
                    # names — running it through ``_resolve_dep`` would
                    # introspect the wrapper closure (no params) and
                    # drop the mapping. The Rust+resolver path handles
                    # this via ``_resolution.py:624``; the in-process
                    # dispatcher used to fall through to ``_resolve_dep``
                    # and produce ``unexpected keyword argument
                    # 'pm_p__p'`` 422s when the synthesized fields then
                    # leaked into the user handler call.
                    if p.get("_is_param_model_builder"):
                        dep_in_map = p.get("dep_input_map") or []
                        builder_kwargs = {}
                        for dest_key, src_key in dep_in_map:
                            if src_key == "__fastapi_turbo_raw_query__":
                                # FA's ``model_validate(raw_query_dict)``
                                # contract: hand the model the wire-key
                                # → raw-value (last-occurrence wins for
                                # scalar, list for repeated). Pydantic
                                # then runs its own validator on the
                                # whole model and surfaces FA-shaped
                                # ``loc=["query","f"]`` errors.
                                rd: dict = {}
                                for qk, qv in _qp_items:
                                    _ex = rd.get(qk)
                                    if _ex is None:
                                        rd[qk] = qv
                                    elif isinstance(_ex, list):
                                        _ex.append(qv)
                                    else:
                                        rd[qk] = [_ex, qv]
                                builder_kwargs[dest_key] = rd
                            elif src_key == "__fastapi_turbo_raw_headers__":
                                rd_h: dict = {}
                                for hk_, hv_ in scope.get("headers", []) or []:
                                    _hk = (
                                        hk_.decode("latin-1")
                                        if isinstance(hk_, bytes)
                                        else hk_
                                    ).lower()
                                    _hv = (
                                        hv_.decode("latin-1")
                                        if isinstance(hv_, bytes)
                                        else hv_
                                    )
                                    _exh = rd_h.get(_hk)
                                    if _exh is None:
                                        rd_h[_hk] = _hv
                                    elif isinstance(_exh, list):
                                        _exh.append(_hv)
                                    else:
                                        rd_h[_hk] = [_exh, _hv]
                                builder_kwargs[dest_key] = rd_h
                            elif src_key == "__fastapi_turbo_raw_cookies__":
                                builder_kwargs[dest_key] = dict(_scope_cookies)
                            elif src_key == "__fastapi_turbo_raw_form__":
                                builder_kwargs[dest_key] = dict(form_fields)
                            else:
                                # Normal extraction-step source — pull
                                # from the kwargs we've already populated.
                                if src_key in kwargs:
                                    builder_kwargs[dest_key] = kwargs[src_key]
                        # Builders are sync; ``model_validate`` is sync.
                        kwargs[name] = dep_callable(**builder_kwargs)
                        continue
                    # Seed with scopes from a top-level ``Security(...)``
                    # marker so the inner-dep ``SecurityScopes`` param
                    # sees them (matches FA semantics). ``_dep_scope``
                    # carries FA 0.120+'s ``Depends(..., scope=...)``
                    # selector so the resolver appends the dep's
                    # generator to the right teardown bucket
                    # (function-scope drains pre-response, request-
                    # scope drains post-response).
                    top_scopes = list(p.get("_security_scopes_top") or [])
                    kwargs[name] = await _resolve_dep(
                        dep_callable, dep_cache, top_scopes,
                        dep_scope=p.get("_dep_scope"),
                        use_cache=p.get("use_cache", True),
                        # Forward the IMMEDIATE Security marker's own
                        # scopes so the cache key flips into "scope-
                        # aware" mode at the leaf — required for
                        # ``test_security_cache`` where two
                        # ``Security(dep, scopes=["scope"])`` should
                        # cache as the same entry but a third
                        # ``Security(dep)`` (no scopes) should be
                        # distinct.
                        _own_scopes_for_key=top_scopes,
                    )
                    continue

                if kind == "path":
                    raw = path_params.get(alias, "")
                    kwargs[name] = _validate(
                        scalar_validator, raw, "path", alias,
                        annotation=p.get("_unwrapped_annotation"),
                    )
                    continue

                if kind == "query":
                    try:
                        # Synthesized field-extraction params for
                        # ``Annotated[Model, Query()]`` skip per-field
                        # scalar validation — the builder dep runs
                        # ``model_validate`` on the raw-string dict
                        # so Pydantic emits all errors with ORIGINAL
                        # raw inputs (FA contract:
                        # ``test_query_param_model_invalid`` expects
                        # ``input: "150"`` not ``input: 150``).
                        _is_synth_field = bool(p.get("_param_model_field_name"))
                        if is_list_param:
                            vals = _extract_list_from_query(alias)
                            if not vals:
                                if required:
                                    _outer_form_missing_errs.append(
                                        _missing("query", alias)
                                    )
                                    continue
                                kwargs[name] = _list_default_for_missing(p, default_val, has_default)
                                continue
                            if _is_synth_field:
                                kwargs[name] = vals
                            else:
                                kwargs[name] = _validate(
                                    scalar_validator, vals, "query", alias,
                                    annotation=p.get("_unwrapped_annotation"),
                                )
                            continue
                        # Scalar query: first occurrence wins.
                        raw = None
                        for k, v in _qp_items:
                            if k == alias:
                                raw = v
                                break
                        if raw is None:
                            if required:
                                _outer_form_missing_errs.append(
                                    _missing("query", alias)
                                )
                                continue
                            kwargs[name] = default_val
                            continue
                        if _is_synth_field:
                            kwargs[name] = raw
                        else:
                            kwargs[name] = _validate(
                                scalar_validator, raw, "query", alias,
                                annotation=p.get("_unwrapped_annotation"),
                            )
                    except _RVE_err as _ve:
                        _outer_form_missing_errs.extend(
                            [
                                {**_e, "loc": list(_e.get("loc") or ())}
                                if isinstance(_e.get("loc"), tuple)
                                else _e
                                for _e in _ve.errors()
                            ]
                        )
                    continue

                if kind == "header":
                    marker = p.get("_raw_marker")
                    hdr_alias = alias
                    if marker is not None and not getattr(marker, "alias", None):
                        hdr_alias = _alias_for_header(marker, name)
                    try:
                        if is_list_param:
                            vals = _scope_headers.getlist(hdr_alias)
                            if not vals:
                                if required:
                                    _outer_form_missing_errs.append(
                                        _missing("header", hdr_alias)
                                    )
                                    continue
                                kwargs[name] = _list_default_for_missing(p, default_val, has_default)
                                continue
                            kwargs[name] = _validate(
                                scalar_validator, vals, "header", hdr_alias,
                                annotation=p.get("_unwrapped_annotation"),
                            )
                            continue
                        raw = _scope_headers.get(hdr_alias)
                        if raw is None:
                            if required:
                                _outer_form_missing_errs.append(
                                    _missing("header", hdr_alias)
                                )
                                continue
                            kwargs[name] = default_val
                            continue
                        kwargs[name] = _validate(
                            scalar_validator, raw, "header", hdr_alias,
                            annotation=p.get("_unwrapped_annotation"),
                        )
                    except _RVE_err as _ve:
                        _outer_form_missing_errs.extend(
                            [
                                {**_e, "loc": list(_e.get("loc") or ())}
                                if isinstance(_e.get("loc"), tuple)
                                else _e
                                for _e in _ve.errors()
                            ]
                        )
                    continue

                if kind == "cookie":
                    try:
                        raw = _scope_cookies.get(alias)
                        if raw is None:
                            if required:
                                _outer_form_missing_errs.append(
                                    _missing("cookie", alias)
                                )
                                continue
                            kwargs[name] = default_val
                            continue
                        kwargs[name] = _validate(
                            scalar_validator, raw, "cookie", alias,
                            annotation=p.get("_unwrapped_annotation"),
                        )
                    except _RVE_err as _ve:
                        _outer_form_missing_errs.extend(
                            [
                                {**_e, "loc": list(_e.get("loc") or ())}
                                if isinstance(_e.get("loc"), tuple)
                                else _e
                                for _e in _ve.errors()
                            ]
                        )
                    continue

                if kind == "body":
                    # ``introspect_endpoint`` collapses multi-body or
                    # ``Body(embed=True)`` endpoints to a single synthetic
                    # ``_combined_body`` param whose ``model_class`` is a
                    # Pydantic model with one field per original body
                    # param. We validate the incoming JSON against that
                    # model, then split the instance back into per-param
                    # kwargs matching the user function signature.
                    from pydantic import ValidationError as _PyVE2
                    is_combined = name == "_combined_body" and model_class is not None
                    if is_combined:
                        # Combined body needs a dict-shaped wire
                        # payload. ``parsed_body=[]`` (empty list) /
                        # other non-dict shapes have no field
                        # mapping — emit per-field missing errors
                        # rather than letting Pydantic produce a
                        # ``model_attributes_type``. Probe-confirmed
                        # against
                        # ``test_body_multiple_params/test_post_body_empty_list``.
                        if parsed_body is not None and not isinstance(parsed_body, dict):
                            _cb_fields_nd = getattr(
                                model_class, "model_fields", {}
                            ) or {}
                            if _cb_fields_nd:
                                raise _RVE_err([
                                    _missing("body", _fn) for _fn in _cb_fields_nd
                                ])
                            raise _RVE_err([_missing("body", name)])
                        if parsed_body is None:
                            if required:
                                # Emit one ``missing`` per body field.
                                # The combined model's ``model_fields``
                                # has exactly the original body param
                                # names ('item', 'user', 'importance',
                                # ...), which is what FA expects in the
                                # 422 detail. Earlier we used
                                # ``sig.parameters`` (the FULL endpoint
                                # signature) which produced a single
                                # ``["body"]`` entry instead.
                                _cb_fields = getattr(
                                    model_class, "model_fields", {}
                                ) or {}

                                def _cb_alias_or_name(_fn):
                                    _fi = _cb_fields.get(_fn)
                                    if _fi is None:
                                        return _fn
                                    _va = getattr(_fi, "validation_alias", None)
                                    if isinstance(_va, str) and _va:
                                        return _va
                                    return getattr(_fi, "alias", None) or _fn
                                if _cb_fields:
                                    raise _RVE_err([
                                        _missing("body", _cb_alias_or_name(_fn))
                                        for _fn in _cb_fields
                                    ])
                                raise _RVE_err([_missing("body", name)])
                            continue
                        try:
                            if hasattr(model_class, "model_validate"):
                                instance = model_class.model_validate(parsed_body)
                            elif hasattr(model_class, "validate_python"):
                                instance = model_class.validate_python(parsed_body)
                            else:
                                instance = parsed_body
                        except _PyVE2 as pve:
                            errs = []
                            def _cb_field_alias_for(_fi, _fn):
                                _va = getattr(_fi, "validation_alias", None)
                                if isinstance(_va, str) and _va:
                                    return _va
                                return getattr(_fi, "alias", None) or _fn
                            _cb_field_aliases = {
                                _fn: _cb_field_alias_for(_fi, _fn)
                                for _fn, _fi in (
                                    getattr(model_class, "model_fields", {}) or {}
                                ).items()
                            }
                            for e in pve.errors():
                                # Preserve ``ctx`` (R39). Remap each
                                # leading loc segment from the
                                # synthetic-model field NAME to the FA
                                # ALIAS so 422 ``loc`` matches the
                                # wire shape — ``["body", "p_alias"]``
                                # for ``Body(alias="p_alias")``, not
                                # the python-side ``["body", "p"]``.
                                new = {k: v for k, v in e.items() if k != "url"}
                                loc = list(new.get("loc", ()))
                                if loc and isinstance(loc[0], str):
                                    loc[0] = _cb_field_aliases.get(loc[0], loc[0])
                                new["loc"] = ["body", *loc] if loc else ["body"]
                                # FA contract: ``input`` is ``None``
                                # ONLY for top-level missing fields
                                # (e.g. ``loc=["body", "user"]`` where
                                # ``user`` is absent from the payload).
                                # Nested missing (``loc=["body", "item",
                                # "price"]``) keeps Pydantic's input —
                                # the partial parent dict — so the
                                # client can see what WAS supplied.
                                # Detect via the original ``loc`` depth
                                # before we prepended ``"body"``.
                                _orig_loc_len = len(list(e.get("loc", ())))
                                if (
                                    new.get("type") == "missing"
                                    and _orig_loc_len <= 1
                                ):
                                    new["input"] = None
                                errs.append(new)
                            raise _RVE_err(errs, body=parsed_body) from None
                        # Split fields back into user-signature kwargs.
                        field_names = getattr(model_class, "model_fields", {}) or {}
                        for field_name in field_names:
                            kwargs[field_name] = getattr(instance, field_name)
                        continue

                    # Simple single body. If no body was provided AND
                    # this param has a default value, use the default
                    # (matches upstream — ``def _i(t: list[str] = []):``
                    # serves an empty request as ``t=[]``, not 422).
                    val = parsed_body
                    if val is None and not required:
                        kwargs[name] = default_val
                        continue
                    # Single-body required missing emits ``loc=["body"]``
                    # (FA's contract: the field name lives in the
                    # endpoint signature, not the wire payload — there
                    # is no aliased "body root" name). The combined-
                    # body path handles the embedded multi-field case.
                    if val is None and required:
                        raise _RVE_err([{
                            "type": "missing",
                            "loc": ["body"],
                            "msg": "Field required",
                            "input": None,
                        }])
                    if model_class is not None:
                        # Pre-check: if the request's content-type
                        # was non-JSON AND val is a raw body string,
                        # AND the target is a Pydantic ``BaseModel``
                        # subclass, raise FA's
                        # ``model_attributes_type`` directly.
                        # Pydantic would emit ``model_type`` with a
                        # different ``msg`` and ``ctx.class_name``
                        # that doesn't match upstream's snapshot.
                        # Probe-confirmed against
                        # ``test_post_form_for_json``. We gate on
                        # the non-JSON CT path only; JSON-shaped
                        # values like a JSON-encoded string or
                        # number must still flow through Pydantic
                        # (which accepts them for str/int/float
                        # typed bodies).
                        try:
                            from pydantic import BaseModel as _PBM_local
                            _mc_is_basemodel = isinstance(model_class, type) and issubclass(model_class, _PBM_local)
                        except Exception:  # noqa: BLE001
                            _mc_is_basemodel = False
                        if (
                            _mc_is_basemodel
                            and not _ct_is_json
                            and isinstance(val, (str, bytes))
                        ):
                            raise _RVE_err([{
                                "type": "model_attributes_type",
                                "loc": ["body"],
                                "msg": (
                                    "Input should be a valid dictionary or "
                                    "object to extract fields from"
                                ),
                                "input": val,
                            }], body=val)
                        try:
                            if hasattr(model_class, "model_validate"):
                                kwargs[name] = model_class.model_validate(val)
                            elif hasattr(model_class, "validate_python"):
                                kwargs[name] = model_class.validate_python(val)
                            else:
                                kwargs[name] = val
                        except _PyVE2 as pve:
                            errs = []
                            for e in pve.errors():
                                # Preserve ``ctx`` (R39).
                                new = {k: v for k, v in e.items() if k != "url"}
                                loc = list(new.get("loc", ()))
                                new["loc"] = ["body", *loc] if loc else ["body"]
                                errs.append(new)
                            raise _RVE_err(errs, body=val) from None
                        continue
                    kwargs[name] = _validate(
                        scalar_validator, val, "body", alias,
                        annotation=p.get("_unwrapped_annotation"),
                    )
                    continue

                if kind in ("form", "file"):
                    val = form_fields.get(alias)
                    # FA emits form/file errors under the ``body`` loc
                    # prefix (forms are classified as body in 422s) —
                    # ``["body", "p"]``, not ``["form", "p"]``.
                    # When the user annotates the file param as
                    # ``bytes`` / ``list[bytes]`` (rather than
                    # ``UploadFile`` / ``list[UploadFile]``), FA reads
                    # the upload content and hands the user the raw
                    # bytes. Detect via the unwrapped annotation.
                    _file_inner_ann = p.get("_unwrapped_annotation")
                    _file_wants_bytes = False
                    if kind == "file":
                        _f_origin = _tp_local.get_origin(_file_inner_ann)
                        if _f_origin is list:
                            _f_args = _tp_local.get_args(_file_inner_ann)
                            if _f_args and _f_args[0] in (bytes, bytearray):
                                _file_wants_bytes = True
                        elif _file_inner_ann in (bytes, bytearray):
                            _file_wants_bytes = True

                    def _uf_to_bytes(uf):
                        # ``UploadFile.file`` is a SpooledTemporaryFile
                        # / BytesIO with the upload contents.
                        f = getattr(uf, "file", None)
                        if f is None:
                            return b""
                        try:
                            f.seek(0)
                            return f.read()
                        except Exception:  # noqa: BLE001
                            return b""

                    if is_list_param:
                        # List form/file: collect into a list. The
                        # parser stores repeated keys as a list, single
                        # values as scalars; normalize so the param
                        # always receives a list.
                        if val is None:
                            if required:
                                _outer_form_missing_errs.append(
                                    _missing("body", alias)
                                )
                                continue
                            kwargs[name] = _list_default_for_missing(p, default_val, has_default)
                            continue
                        vals = val if isinstance(val, list) else [val]
                        if kind == "file":
                            if _file_wants_bytes:
                                kwargs[name] = [_uf_to_bytes(_v) for _v in vals]
                            else:
                                kwargs[name] = vals
                        else:
                            kwargs[name] = _validate(
                                p.get("scalar_validator"),
                                vals,
                                "body",
                                alias,
                                annotation=p.get("_unwrapped_annotation"),
                            )
                        continue
                    if val is None:
                        if required:
                            _outer_form_missing_errs.append(
                                _missing("body", alias)
                            )
                            continue
                        kwargs[name] = default_val
                    else:
                        # FA contract: an empty form / file value with
                        # an Optional annotation falls back to the
                        # param's default (``age: Optional[int] =
                        # Form() = None`` returns ``None``, not 422).
                        # Probe-confirmed against
                        # ``test_form_default_url_encoded`` /
                        # ``_multi_part``.
                        _is_empty = val == "" or val == b""
                        if _is_empty and not required:
                            kwargs[name] = default_val
                            continue
                        if kind == "file":
                            # File uploads stay raw — they're already
                            # an ``UploadFile`` object. If the parser
                            # produced a list (multiple files under
                            # the same field name) but the param is
                            # scalar, take the first to preserve FA
                            # parity.
                            if isinstance(val, list):
                                val = val[0]
                            if _file_wants_bytes:
                                kwargs[name] = _uf_to_bytes(val)
                            else:
                                kwargs[name] = val
                        else:
                            # Coerce the form value to the parameter's
                            # declared type via Pydantic. Without this
                            # ``age: int = Form(...)`` would receive
                            # the string ``"30"`` rather than ``30``.
                            if isinstance(val, list):
                                val = val[0]
                            kwargs[name] = _validate(
                                p.get("scalar_validator"),
                                val,
                                "body",
                                alias,
                                annotation=p.get("_unwrapped_annotation"),
                            )
                    continue

                if kind in ("inject_request",):
                    # Pass ``receive`` so ``request.is_disconnected()``
                    # can peek the ASGI channel for ``http.disconnect``
                    # messages. Without this, every Request injected
                    # as a kwarg has ``_receive=None`` and
                    # ``is_disconnected`` is unconditionally ``False``.
                    kwargs[name] = _Req(req_scope, receive=receive)
                    continue
                if kind in ("inject_response",):
                    # Share the SAME instance with any deps that asked
                    # for ``response: Response``. ``_get_or_create_
                    # response_inject`` lazy-allocates and stores in
                    # ``_shared_response_holder[0]``; the dep resolver
                    # reads from the same slot. Without this, deps had
                    # their own (different) Response instance and any
                    # headers they set didn't reach the handler's
                    # response.
                    resp_inst = _get_or_create_response_inject()
                    response_injected = resp_inst
                    kwargs[name] = resp_inst
                    continue
                if kind in ("inject_background_tasks",):
                    if _bg_holder[0] is None:
                        bg_inst = _BGT()
                        bg_inst._app = self
                        _bg_holder[0] = bg_inst
                    bg_injected = _bg_holder[0]
                    kwargs[name] = bg_injected
                    continue

                # Unknown kind — skip (defer to endpoint default).
            if _outer_form_missing_errs:
                raise _RVE_err(_outer_form_missing_errs)
        except Exception as exc:
            # Any param-resolution failure → route through the app's
            # exception handlers. This includes RequestValidationError
            # (→ 422) and HTTPException from a dep (→ its status).
            # If the exception is a RequestValidationError raised
            # without endpoint context, augment it so user
            # ``@app.exception_handler(RequestValidationError)``
            # implementations can log file / line / function alongside
            # the validation errors. Matches FA's behaviour and
            # upstream's ``test_validation_error_context`` suite.
            if isinstance(exc, _RVE_err) and not getattr(exc, "endpoint_ctx", None):
                try:
                    _ep_for_ctx = endpoint
                    _ep_func = getattr(_ep_for_ctx, "__name__", None)
                    _ep_file = getattr(
                        getattr(_ep_for_ctx, "__code__", None), "co_filename", None
                    )
                    _ep_line = getattr(
                        getattr(_ep_for_ctx, "__code__", None), "co_firstlineno", None
                    )
                    exc.endpoint_ctx = {
                        "function": _ep_func,
                        "file": _ep_file,
                        "line": _ep_line,
                        "path": getattr(matched_route, "path", None),
                    }
                    exc.endpoint_function = _ep_func
                    exc.endpoint_file = _ep_file
                    exc.endpoint_line = _ep_line
                    exc.endpoint_path = getattr(matched_route, "path", None)
                except Exception:  # noqa: BLE001
                    pass
            await _asgi_emit_exception(self, scope, send, exc)
            # Teardown any deps already committed.
            for gen, is_async, _td_scope in reversed(dep_teardowns):
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
                except Exception:  # noqa: BLE001
                    pass
            return True

        # Invoke the endpoint, wrapped in any ``@app.middleware('http')``
        # functions. ``_http_middlewares`` is stored in declaration
        # order; FA semantics: last-decorated is outermost. We mirror
        # that by wrapping innermost first.
        #
        # Skip entries that are ASGI-middleware shims — those are
        # ``(request, call_next)`` adapters around raw-ASGI classes
        # that already ran via the ``_raw_asgi_middlewares`` chain
        # at the top of this function. Running the shim here would
        # fire the MW a second time.
        http_mws = [
            m
            for m in (getattr(self, "_http_middlewares", None) or [])
            if not getattr(m, "_fastapi_turbo_is_asgi_shim", False)
        ]

        # Pull response_model + options off the matched route so we
        # filter + alias the return value the same way the Rust hot
        # path does. The route object carries everything _try_compile_
        # handler would normally honour.
        _route = matched_route
        _resp_model = getattr(_route, "response_model", None)
        _rm_opts = {
            "include": getattr(_route, "response_model_include", None),
            "exclude": getattr(_route, "response_model_exclude", None),
            "exclude_unset": getattr(_route, "response_model_exclude_unset", False),
            "exclude_defaults": getattr(_route, "response_model_exclude_defaults", False),
            "exclude_none": getattr(_route, "response_model_exclude_none", False),
            "by_alias": getattr(_route, "response_model_by_alias", True),
            # Carry endpoint context (function name / file / line /
            # path) into the response_model validator so any
            # ``ResponseValidationError`` it raises shows ``in
            # <endpoint>`` in the user's exception_handler — matches
            # FA's ``test_validation_error_context`` suite.
            "endpoint_ctx": {
                "function": getattr(endpoint, "__name__", None),
                "file": getattr(
                    getattr(endpoint, "__code__", None), "co_filename", None
                ),
                "line": getattr(
                    getattr(endpoint, "__code__", None), "co_firstlineno", None
                ),
                "path": getattr(_route, "path", None),
            },
        }
        _status_code = getattr(_route, "status_code", None)

        # Filter ``kwargs`` to ONLY the keys the user handler actually
        # takes. The synthesized field-extraction params from
        # ``_maybe_expand_param_models`` (``pm_p__p`` etc.) feed the
        # builder dep, not the handler. Without this filter the user
        # function got hit with ``unexpected keyword argument
        # 'pm_p__p'``. The endpoint signature is the source of truth —
        # introspect_params has placeholders like ``_combined_body``
        # that don't appear on the user fn, while the body splitter
        # writes back to the original names (``qty`` etc.) that DO.
        try:
            _ep_sig_local = _insp.signature(endpoint)
            _accepts_var_kw = any(
                _pp.kind is _insp.Parameter.VAR_KEYWORD
                for _pp in _ep_sig_local.parameters.values()
            )
            if not _accepts_var_kw:
                _ep_param_names = set(_ep_sig_local.parameters.keys())
                kwargs = {
                    _kk: _vv for _kk, _vv in kwargs.items()
                    if _kk in _ep_param_names
                }
        except (TypeError, ValueError):
            # Builtin / C function with no introspectable signature —
            # leave kwargs as-is so the runtime call still surfaces a
            # meaningful error.
            pass

        async def _call_endpoint(_request):
            """Invoke the endpoint + apply response_model. Exceptions
            propagate — the outer envelope routes them through the
            app's exception_handlers."""
            from fastapi_turbo._route_helpers import _apply_response_model
            # We assign to ``response_injected`` below if a dep
            # injected one but the handler didn't take a Response
            # parameter. Mark it nonlocal so the assignment doesn't
            # turn the name into a fresh local (which would shadow
            # the outer scope's value and trip UnboundLocalError on
            # the read-before-assign at line 8423).
            nonlocal response_injected

            if _insp.iscoroutinefunction(endpoint):
                r = await endpoint(**kwargs)
            else:
                r = endpoint(**kwargs)
                if _insp.iscoroutine(r):
                    r = await r
            if isinstance(r, _Resp):
                return r
            # FA 0.110+ async-generator / generator handlers are auto-
            # wrapped. Respect ``response_class`` if the user pinned a
            # streaming-friendly one (e.g. ``EventSourceResponse`` for
            # SSE) — pass the generator through unchanged so the
            # response class can drive its own framing. Otherwise
            # default to NDJSON (FA's stream_json_lines tutorial). The
            # Rust hot path detects this at compile time; the
            # in-process dispatcher used to fall through to
            # ``jsonable_encoder`` which raised ``'async_generator'
            # object is not iterable``.
            if _insp.isasyncgen(r) or _insp.isgenerator(r):
                from fastapi_turbo.responses import StreamingResponse as _SR
                _route_resp_class = getattr(
                    matched_route, "response_class", None
                )
                _has_custom_resp_class = (
                    _route_resp_class is not None
                    and _route_resp_class is not _Resp
                    and _route_resp_class is not _JR
                    and not _is_json_response_class(_route_resp_class)
                )
                if _has_custom_resp_class:
                    # Hand the generator to the user-pinned response
                    # class; SSE / custom streamers know what to do.
                    return _route_resp_class(r)
                import json as _json_for_ndjson

                async def _ndjson_iter(_gen):
                    if _insp.isasyncgen(_gen):
                        async for _item in _gen:
                            yield (
                                _json_for_ndjson.dumps(
                                    _je(_item), separators=(",", ":"),
                                ).encode("utf-8") + b"\n"
                            )
                    else:
                        for _item in _gen:
                            yield (
                                _json_for_ndjson.dumps(
                                    _je(_item), separators=(",", ":"),
                                ).encode("utf-8") + b"\n"
                            )
                return _SR(
                    _ndjson_iter(r),
                    media_type="application/jsonl",
                )
            # Apply response_model filtering / aliasing / exclude-unset.
            # Errors from this path must propagate — FA surfaces
            # ResponseValidationError as a 500; silently returning the
            # unvalidated payload is a security hole (we'd leak fields
            # the schema intended to strip).
            if _resp_model is not None:
                r = _apply_response_model(r, _resp_model, **_rm_opts)
            status_code = _status_code or 200
            # Resolve response class: route → app default → JSONResponse.
            # Without this cascade, ``default_response_class=HTMLResponse``
            # on a FastAPI app would silently fall back to JSONResponse
            # for handlers that return raw strings.
            response_class = _resolve_response_class(matched_route, self)
            # If a dep / handler injected ``response: Response`` and
            # set ``response.status_code`` to a body-bearing code,
            # the override beats the route's default. Probe-confirmed
            # against ``test_reponse_set_reponse_code_empty``: a
            # route with ``status_code=204`` whose handler does
            # ``response.status_code = 400`` must serve the body
            # (not the empty 204 path).
            _eff_status = status_code
            _ri_for_status = response_injected
            if _ri_for_status is None and _shared_response_holder[0] is not None:
                _ri_for_status = _shared_response_holder[0]
            if _ri_for_status is not None:
                _ri_status = getattr(_ri_for_status, "status_code", None)
                if _ri_status:
                    _eff_status = _ri_status
            # No-body status codes (1xx / 204 / 304): RFC 9110 forbids
            # a body. FA returns an empty Response regardless of what
            # the handler returned (typically ``None`` / ``pass``).
            # Probe-confirmed: ``test_response_code_no_body`` expects
            # ``response.content == b""`` and no ``content-length``
            # header.
            if _eff_status in (204, 304) or 100 <= _eff_status < 200:
                from fastapi_turbo.responses import Response as _PR
                out = _PR(status_code=_eff_status)
                # Strip default content-type/length so the assertion
                # ``"content-length" not in response.headers`` holds.
                try:
                    if "content-length" in out.headers:
                        del out.headers["content-length"]
                except Exception:  # noqa: BLE001
                    pass
            elif response_class is _JR or _is_json_response_class(response_class):
                out = response_class(content=_je(r), status_code=_eff_status)
            else:
                # Some response classes don't take ``content=`` —
                # ``RedirectResponse`` takes ``url=``, ``FileResponse``
                # takes ``path=``. The handler returns the URL /
                # path string. Detect via the constructor signature
                # and re-route the kwarg.
                _rc_sig_params = ()
                try:
                    _rc_sig_params = tuple(
                        _insp.signature(response_class).parameters.keys()
                    )
                except (TypeError, ValueError):
                    pass
                # Pass ``status_code`` only when the route
                # explicitly set one; otherwise let the response
                # class use its own default (``RedirectResponse``
                # defaults to 307, ``FileResponse`` to 200).
                _explicit_status_kwargs = (
                    {"status_code": _eff_status}
                    if _status_code or (_ri_for_status is not None and getattr(_ri_for_status, "status_code", None))
                    else {}
                )
                if "content" in _rc_sig_params:
                    out = response_class(content=r, **_explicit_status_kwargs)
                elif "url" in _rc_sig_params:
                    out = response_class(url=r, **_explicit_status_kwargs)
                elif "path" in _rc_sig_params:
                    out = response_class(path=r, **_explicit_status_kwargs)
                else:
                    out = response_class(r, **_explicit_status_kwargs)
            # Pull any dep-mutated Response even when the handler itself
            # didn't take one — deps often add headers via
            # ``response: Response`` while the handler returns a
            # plain dict. Without this fold, those header mutations
            # got dropped on the floor.
            if response_injected is None and _shared_response_holder[0] is not None:
                response_injected = _shared_response_holder[0]
            if response_injected is not None:
                # Iterate ``headers`` (the dict view) AND ``raw_headers``
                # so we catch both ``response.headers["k"] = "v"``
                # (which only updates the dict) AND
                # ``response.headers.append("k", "v")`` (which also
                # appends to raw_headers).
                _seen_dup = set()
                for k, v in (
                    getattr(response_injected, "headers", {}) or {}
                ).items():
                    out.headers[k] = v
                    _seen_dup.add((k, str(v)))
                for k, v in getattr(response_injected, "raw_headers", []) or []:
                    if (k, v) in _seen_dup:
                        continue
                    out.raw_headers.append((k, v))
                if getattr(response_injected, "status_code", None):
                    out.status_code = response_injected.status_code
            return out

        # Build the middleware chain. Each element is
        # ``async def mw(request, call_next) -> Response``. We compose
        # so ``call_next`` invokes the next inner MW, or finally the
        # endpoint.
        #
        # FA convention: last-registered middleware is outermost
        # (handles the request first). ``_http_middlewares`` is
        # appended-to in registration order, so iterating FORWARD
        # gets us there: the last item processed becomes the
        # outermost wrapper. Iterating reversed (the previous code)
        # made FIRST-registered outermost, breaking ordering parity.
        current_call = _call_endpoint
        if http_mws:
            for mw in http_mws:
                _inner = current_call

                async def _wrapped(req, *, _mw=mw, _inner=_inner):
                    return await _mw(req, _inner)

                current_call = _wrapped

        # Wrap ``receive`` so the body bytes the dispatcher already
        # drained aren't redelivered (the handler reads them via
        # ``request.body()`` / ``request._body`` from
        # ``_fastapi_turbo_prebuffered_body``). Subsequent ``receive``
        # calls (e.g. from ``request.is_disconnected()``) pass through
        # to the real ASGI receive, surfacing
        # ``{"type": "http.disconnect"}`` when the client drops.
        async def _ws_aware_receive():
            return await receive()

        req_obj = _Req(req_scope, receive=_ws_aware_receive)
        try:
            # Honour ``FastAPI(worker_timeout=…)`` for the in-process
            # dispatch path. Without this the dispatcher just awaited
            # the endpoint forever, returning a spurious 200 with the
            # slow handler's eventual output. ``asyncio.wait_for``
            # cancels the task at the deadline and raises
            # ``TimeoutError`` — we surface that as 504 below.
            _wt = getattr(self, "worker_timeout", None)
            if _wt is not None and _wt > 0:
                result = await asyncio.wait_for(current_call(req_obj), timeout=_wt)
            else:
                result = await current_call(req_obj)
            # FA 0.120+: drain ``scope='function'`` teardowns
            # IMMEDIATELY after the handler returns, before the
            # response is built. Function-scope teardowns are allowed
            # to raise (e.g. an HTTPException from
            # ``raise_after_yield``) and the new exception aborts the
            # response with the user's status code.
            _func_scope_remaining: list = []
            _request_scope_remaining: list = []
            for _t in dep_teardowns:
                if _t[2] == "function":
                    _func_scope_remaining.append(_t)
                else:
                    _request_scope_remaining.append(_t)
            for gen, is_async, _td_scope in reversed(_func_scope_remaining):
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
            # The drained teardowns should not run again post-
            # response — keep only request-scope on the deferred list.
            dep_teardowns[:] = _request_scope_remaining
        except asyncio.TimeoutError:
            # Drain dep teardowns then surface 504.
            for gen, is_async, _td_scope in reversed(dep_teardowns):
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
                except Exception:  # noqa: BLE001
                    pass
            await send({
                "type": "http.response.start",
                "status": 504,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", b"15"),
                ],
            })
            await send({"type": "http.response.body", "body": b"Gateway Timeout"})
            return True
        except Exception as exc:
            # Endpoint / middleware / response_model raised. Route
            # through the app's exception_handlers — this honours a
            # user's `@app.exception_handler(HTTPException)` override
            # and surfaces ResponseValidationError / RequestValidationError
            # with FA-shaped bodies.
            #
            # IMPORTANT: do dep teardowns BEFORE
            # ``_asgi_emit_exception`` because that helper re-raises
            # for the unhandled-Exception case (so
            # ``raise_server_exceptions=True`` propagates), which
            # would otherwise unwind us past the teardown loop. Push
            # the original exception into each active yield-dep via
            # ``gen.throw(...)`` / ``gen.athrow(...)`` so a dep
            # wrapping the request in ``try / except / finally`` sees
            # the failure and runs its cleanup code. Earlier impl
            # drove plain ``__anext__()`` AFTER ``_asgi_emit_exception``
            # — finally blocks never fired in the unhandled-error
            # case (probe-confirmed against upstream's
            # ``test_dependency_contextmanager`` suite, ~16 tests).
            # FA 0.110+ contract: a yield-dep that catches the
            # handler's exception WITHOUT re-raising must trip a
            # ``FastAPIError("...raising an exception and a dependency
            # with yield...")``. Detect by the throw outcome:
            #   * ``StopIteration`` / ``StopAsyncIteration`` (or
            #     ``StopIteration`` raised by ``athrow`` returning
            #     normally on a fully-exhausted gen) means the dep's
            #     except arm swallowed the exception silently — raise
            #     ``FastAPIError`` so the user can see the bug.
            #   * The original exception type (or any other) means the
            #     dep re-raised; the original exception propagates as
            #     intended.
            from fastapi_turbo.exceptions import FastAPIError as _FAPIErr
            _yielddep_swallowed = False
            # If a dep's ``except`` arm raises a NEW exception (e.g.
            # ``except CustomError: raise HTTPException(418)``), that
            # new exception SUPERSEDES the original handler exception.
            # FA's contract: the response uses the most-recent thrown
            # exception, not the handler's. Probe-confirmed against
            # ``test_dependency_after_yield_raise::test_catching``.
            _replacement_exc = None
            for gen, is_async, _td_scope in reversed(dep_teardowns):
                try:
                    if is_async:
                        try:
                            await gen.athrow(exc)
                        except StopAsyncIteration:
                            _yielddep_swallowed = True
                        except BaseException as _new_exc:  # noqa: BLE001
                            if _new_exc is exc or isinstance(_new_exc, type(exc)):
                                pass
                            else:
                                _replacement_exc = _new_exc
                    else:
                        try:
                            gen.throw(exc)
                        except StopIteration:
                            _yielddep_swallowed = True
                        except BaseException as _new_exc:  # noqa: BLE001
                            if _new_exc is exc or isinstance(_new_exc, type(exc)):
                                pass
                            else:
                                _replacement_exc = _new_exc
                except Exception:  # noqa: BLE001
                    pass
            dep_teardowns.clear()
            if _replacement_exc is not None:
                exc = _replacement_exc
            elif _yielddep_swallowed:
                exc = _FAPIErr(
                    "Dependency raising an exception and a dependency with"
                    " yield without raising again the same exception or"
                    " a new one"
                )
            await _asgi_emit_exception(self, scope, send, exc)
            for gen, is_async, _td_scope in reversed(dep_teardowns):
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
                except Exception:  # noqa: BLE001
                    pass
            return True

        # ``result`` is already a Response (either the MW-wrapped or
        # the raw endpoint return converted by ``_call_endpoint``).
        # Thread scope through so FileResponse can honour ``Range:``.
        if isinstance(result, _Resp):
            await _send_asgi_response(send, result, scope=scope)
        else:
            # Resolve the response class via the same cascade used in
            # ``_call_endpoint`` above (route → router → include →
            # app → JSONResponse). No try/except — the response_class
            # constructor's failure must surface as a real exception
            # (handled by the app's exception_handlers as a 500),
            # never as a silent 200 JSON envelope.
            response_class = _resolve_response_class(matched_route, self)
            if response_class is _JR or _is_json_response_class(response_class):
                final = response_class(content=_je(result))
            else:
                final = response_class(content=result)
            # Pull any dep-mutated Response even when the handler itself
            # didn't take one — deps often add headers via
            # ``response: Response`` while the handler returns a
            # plain dict. Without this fold, those header mutations
            # got dropped on the floor.
            if response_injected is None and _shared_response_holder[0] is not None:
                response_injected = _shared_response_holder[0]
            if response_injected is not None:
                # Iterate ``headers`` (the dict view) AND ``raw_headers``
                # so we catch both ``response.headers["k"] = "v"``
                # (which only updates the dict) AND
                # ``response.headers.append("k", "v")`` (which also
                # appends to raw_headers).
                _seen_dup = set()
                for k, v in (
                    getattr(response_injected, "headers", {}) or {}
                ).items():
                    final.headers[k] = v
                    _seen_dup.add((k, str(v)))
                for k, v in getattr(response_injected, "raw_headers", []) or []:
                    if (k, v) in _seen_dup:
                        continue
                    final.raw_headers.append((k, v))
                if getattr(response_injected, "status_code", None):
                    final.status_code = response_injected.status_code
            await _send_asgi_response(send, final, scope=scope)
        # FA / Starlette ordering: BG tasks run BEFORE yield-dep
        # teardowns. The contract is that ``BackgroundTasks`` see the
        # request's deps still in their pre-yield ("started") state —
        # tests that read state inside the bg task expect ``a:
        # started a / b: started b``, not ``a: finished a / b:
        # finished b``. Reversed earlier (teardowns first) made bg see
        # post-finalised state and broke
        # ``test_dependency_contextmanager::test_background_tasks``.
        # Pull the shared BG holder if a dep injected it (e.g. via
        # ``Annotated[BackgroundTasks, Depends(add_bg)]``) — without
        # this, ``bg_injected`` is only set when the handler took
        # ``BackgroundTasks`` directly, and dep-added tasks would
        # never run. Probe-confirmed against
        # ``test_response_dependency::test_background_tasks_with_
        # depends_annotated``.
        if bg_injected is None and _bg_holder[0] is not None:
            bg_injected = _bg_holder[0]
        if bg_injected is not None:
            try:
                bg_injected.run_sync()
            except Exception as _exc:  # noqa: BLE001
                _log.debug("in-process background task: %r", _exc)
        # Run yield-dep teardowns in reverse order (LIFO — FA semantics).
        # These fire after the response has been sent. FA's contract:
        # the FIRST exception raised by a teardown propagates out of
        # the dispatcher so the TestClient's ``raise_server_
        # exceptions=True`` path sees it (the response is already
        # flushed; this is purely for in-process error visibility).
        # Probe-confirmed against
        # ``test_dependency_after_yield_raise::test_broken_raise``.
        _post_teardown_exc: BaseException | None = None
        for gen, is_async, _td_scope in reversed(dep_teardowns):
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
            except BaseException as _exc:  # noqa: BLE001
                _log.debug("in-process yield-dep teardown: %r", _exc)
                if _post_teardown_exc is None:
                    _post_teardown_exc = _exc
        if _post_teardown_exc is not None:
            raise _post_teardown_exc
        return True

    # ── in-process WebSocket dispatch ────────────────────────────────

    async def _asgi_dispatch_ws_in_process(
        self, scope: dict, receive: Callable, send: Callable
    ) -> bool:
        """Route an ASGI ``websocket`` scope to a matching @app.websocket
        endpoint without binding a loopback socket.

        Builds a minimal ``WebSocket`` object that bridges the user
        endpoint's ``accept / receive_text / send_text / close`` calls
        to the ASGI ``receive`` / ``send`` channels. Supports the
        common user-facing API (accept headers/subprotocol, text/bytes
        send+receive, receive_json, close codes).

        Returns True when dispatched (the user endpoint ran); False
        when we couldn't match a WS route — caller falls back to the
        loopback proxy.
        """
        import re as _re_ws
        import inspect as _insp_ws

        path = scope.get("path", "/")

        # Route match — scan router routes for websocket entries.
        # Our APIRouter marks WS routes with ``_is_websocket = True``.
        matched_route = None
        path_params: dict = {}
        for route in getattr(self.router, "routes", []) or []:
            if not getattr(route, "_is_websocket", False):
                continue
            r_path = getattr(route, "path", None)
            if not r_path:
                continue
            regex = getattr(route, "_fastapi_turbo_asgi_ws_regex", None)
            if regex is None:
                pattern = "^"
                idx = 0
                for m in _re_ws.finditer(r"\{([^{}:]+)(?::([^{}]+))?\}", r_path):
                    pattern += _re_ws.escape(r_path[idx:m.start()])
                    pname = m.group(1)
                    if m.group(2) == "path":
                        pattern += f"(?P<{pname}>.+)"
                    else:
                        pattern += f"(?P<{pname}>[^/]+)"
                    idx = m.end()
                pattern += _re_ws.escape(r_path[idx:]) + "$"
                regex = _re_ws.compile(pattern)
                try:
                    route._fastapi_turbo_asgi_ws_regex = regex  # type: ignore[attr-defined]
                except (AttributeError, TypeError):
                    pass
            match = regex.match(path)
            if match is None:
                continue
            matched_route = route
            path_params = match.groupdict()
            break

        if matched_route is None:
            # No matching WS route — Starlette closes with 1000
            # (normal closure) when no matching endpoint accepts.
            # Probe-confirmed against
            # ``test_route_scope::test_websocket_invalid_path_doesnt_match``
            # AND ``test_ws_router::test_no_router``.
            try:
                await send({"type": "websocket.close", "code": 1000})
            except Exception:  # noqa: BLE001
                pass
            return True

        endpoint = getattr(matched_route, "endpoint", None)
        if endpoint is None:
            return False
        # Surface the matched route on the WS scope so handlers can
        # read ``websocket.scope["route"].path`` (FA contract — used
        # by Sentry tracing and ``test_route_scope::test_websocket``).
        scope["route"] = matched_route

        # WebSocket shim built on the ASGI receive/send channels. Now
        # supports:
        #   * ``state`` backed by ``scope['state']`` (so middleware /
        #     endpoint share state, matching Starlette).
        #   * ``query_params`` parsed from the scope query_string.
        #   * ``iter_text`` / ``iter_bytes`` / ``iter_json`` async
        #     generators that yield until the client disconnects.
        #   * ``url`` exposed as ``URL`` so ``websocket.url.path``
        #     works (FA tests use that).
        # The user endpoint dispatch path now goes through a minimal
        # introspection-driven param resolver that handles
        # ``Depends(...)``, ``Query(...)``, ``Header(...)``, ``Cookie(...)``,
        # path params, and the WebSocket-typed param itself. That
        # closes the gap with FA where ``websocket: WebSocket, room:
        # str, token=Depends(get_token)`` is a common pattern.
        from fastapi_turbo.exceptions import (
            WebSocketDisconnect as _WSD,
            WebSocketException as _WSE,
        )

        class _InProcessWS:
            def __init__(ws_self):
                ws_self._asgi_receive = receive
                ws_self._asgi_send = send
                ws_self._scope = scope
                ws_self.path_params = path_params
                ws_self._accepted = False
                ws_self._closed = False
                from fastapi_turbo.datastructures import (
                    Headers as _Hdr,
                    URL as _URL,
                    QueryParams as _QP,
                    State as _State,
                )
                ws_self.headers = _Hdr(scope.get("headers", []))
                # Build a Starlette-compatible URL object so
                # ``ws.url.path`` / ``ws.url.query`` work.
                _path = scope.get("path", "/")
                _qs = scope.get("query_string", b"")
                if isinstance(_qs, (bytes, bytearray)):
                    _qs_str = _qs.decode("latin-1")
                else:
                    _qs_str = str(_qs)
                _url_str = f"ws://testserver{_path}"
                if _qs_str:
                    _url_str = f"{_url_str}?{_qs_str}"
                try:
                    ws_self.url = _URL(_url_str)
                except Exception:  # noqa: BLE001
                    ws_self.url = _path
                ws_self.query_params = _QP(_qs_str)
                ws_self.scope = scope
                ws_self._state_cls = _State

            @property
            def state(ws_self):
                """``websocket.state`` shared with the scope so
                middleware mutations propagate (Starlette parity)."""
                existing = ws_self._scope.get("state")
                if isinstance(existing, ws_self._state_cls):
                    return existing
                s = ws_self._state_cls()
                ws_self._scope["state"] = s
                return s

            @property
            def app(ws_self):
                return ws_self._scope.get("app")

            async def accept(ws_self, subprotocol=None, headers=None):
                msg = await ws_self._asgi_receive()
                if msg.get("type") != "websocket.connect":
                    ws_self._closed = True
                    return
                await ws_self._asgi_send({
                    "type": "websocket.accept",
                    "subprotocol": subprotocol,
                    "headers": headers or [],
                })
                ws_self._accepted = True

            async def receive(ws_self):
                return await ws_self._asgi_receive()

            async def receive_text(ws_self):
                msg = await ws_self._asgi_receive()
                if msg.get("type") == "websocket.disconnect":
                    raise _WSD(code=msg.get("code", 1000))
                return msg.get("text", "")

            async def receive_bytes(ws_self):
                msg = await ws_self._asgi_receive()
                if msg.get("type") == "websocket.disconnect":
                    raise _WSD(code=msg.get("code", 1000))
                return msg.get("bytes", b"")

            async def receive_json(ws_self, mode: str = "text"):
                import json as _json
                if mode == "binary":
                    return _json.loads(await ws_self.receive_bytes())
                return _json.loads(await ws_self.receive_text())

            async def iter_text(ws_self):
                try:
                    while True:
                        yield await ws_self.receive_text()
                except _WSD:
                    return

            async def iter_bytes(ws_self):
                try:
                    while True:
                        yield await ws_self.receive_bytes()
                except _WSD:
                    return

            async def iter_json(ws_self):
                try:
                    while True:
                        yield await ws_self.receive_json()
                except _WSD:
                    return

            async def send_text(ws_self, text):
                await ws_self._asgi_send({
                    "type": "websocket.send",
                    "text": text,
                })

            async def send_bytes(ws_self, data):
                await ws_self._asgi_send({
                    "type": "websocket.send",
                    "bytes": data,
                })

            async def send_json(ws_self, obj, mode: str = "text"):
                import json as _json
                encoded = _json.dumps(obj)
                if mode == "binary":
                    await ws_self.send_bytes(encoded.encode("utf-8"))
                else:
                    await ws_self.send_text(encoded)

            async def close(ws_self, code=1000, reason=""):
                if ws_self._closed:
                    return
                await ws_self._asgi_send({
                    "type": "websocket.close",
                    "code": code,
                    "reason": reason,
                })
                ws_self._closed = True

        ws_obj = _InProcessWS()

        # Build kwargs from the endpoint signature using the same
        # introspection the HTTP dispatcher uses, so ``Depends(...)`` /
        # ``Query(...)`` / ``Header(...)`` / ``Cookie(...)`` / path
        # params all work — not just bare WebSocket + path positional.
        from fastapi_turbo._introspect import introspect_endpoint
        from fastapi_turbo.dependencies import Depends as _Dep_marker_ws
        from fastapi_turbo.websockets import WebSocket as _WS_cls

        try:
            ws_introspect_params = introspect_endpoint(
                getattr(endpoint, "_fastapi_turbo_original_endpoint", endpoint),
                getattr(matched_route, "path", "/") or "/",
            )
        except Exception:  # noqa: BLE001
            ws_introspect_params = []

        try:
            sig = _insp_ws.signature(
                getattr(endpoint, "_fastapi_turbo_original_endpoint", endpoint)
            )
        except (TypeError, ValueError):
            return False

        kwargs: dict = {}
        # Identify which parameter is the WebSocket itself (by
        # annotation or by name fallback).
        ws_param_name = None
        for pname, p in sig.parameters.items():
            ann = p.annotation
            if isinstance(ann, type) and issubclass(ann, _WS_cls):
                ws_param_name = pname
                break
        if ws_param_name is None:
            # First positional parameter convention (FastAPI tutorial
            # style: ``async def ws(websocket: WebSocket, ...)``).
            for pname, p in sig.parameters.items():
                if p.kind in (
                    _insp_ws.Parameter.POSITIONAL_ONLY,
                    _insp_ws.Parameter.POSITIONAL_OR_KEYWORD,
                ):
                    ws_param_name = pname
                    break
        if ws_param_name is not None:
            kwargs[ws_param_name] = ws_obj

        # Header / cookie scope for query helpers.
        from fastapi_turbo.datastructures import (
            Headers as _Hdr_ws,
            QueryParams as _QP_ws,
        )
        _ws_headers = _Hdr_ws(scope.get("headers", []))
        _ws_qp = ws_obj.query_params

        # Pydantic-driven scalar coercion for path / query / header
        # params: ``room: int`` should arrive as ``int``, not the
        # raw ``str`` from the URL template / query string. Mirrors
        # the HTTP path's behaviour and matches upstream FastAPI.
        from pydantic import (
            TypeAdapter as _WS_TA,
            ValidationError as _WS_PyVE,
        )
        from fastapi_turbo.param_functions import _ParamMarker as _PM_ws

        def _coerce_to(ann, raw):
            """Coerce ``raw`` to ``ann`` via a Pydantic TypeAdapter.
            ``ann is None`` / ``str`` / ``inspect.Parameter.empty``
            short-circuits to ``raw``. ``ValidationError`` propagates
            so the outer try block closes the WS with 1008."""
            if ann is None or ann is str or ann is _insp_ws.Parameter.empty:
                return raw
            try:
                return _WS_TA(ann).validate_python(raw)
            except _WS_PyVE:
                raise

        def _ws_coerce(p, raw):
            """Apply the param's ``scalar_validator`` (a Pydantic
            ``TypeAdapter``) to ``raw``. Falls back to the unwrapped
            annotation if introspect didn't pre-build one."""
            adapter = p.get("scalar_validator")
            ann = p.get("_unwrapped_annotation")
            if adapter is None and ann is not None and ann not in (str, type(None)):
                try:
                    adapter = _WS_TA(ann)
                except Exception:  # noqa: BLE001
                    adapter = None
            if adapter is None:
                return raw
            return adapter.validate_python(raw)

        def _ws_required_missing(name: str) -> "_WSE":
            """Build a ``WebSocketException(1008)`` for a missing
            required parameter. Caller raises so the outer try
            closes with the user-facing close code."""
            return _WSE(code=1008, reason=f"missing required parameter: {name}")

        def _marker_kind(marker) -> str | None:
            """Return ``"query"`` / ``"header"`` / ``"path"`` /
            ``"cookie"`` for a Query / Header / Path / Cookie
            marker; ``None`` for anything else."""
            if not isinstance(marker, _PM_ws):
                return None
            return getattr(marker, "_kind", None)

        def _marker_is_required(marker) -> bool:
            """``Query(...)`` / ``Header(...)`` with no default — the
            marker's ``default`` attribute is ``Ellipsis`` or
            ``PydanticUndefined``. Anything else means the user
            supplied a default (``Query(7)`` etc.)."""
            d = getattr(marker, "default", None)
            if d is ... or d is _insp_ws.Parameter.empty:
                return True
            try:
                from pydantic_core import PydanticUndefined
                if d is PydanticUndefined:
                    return True
            except ImportError:
                pass
            return False

        def _resolve_marker_value(marker, alias_or_name: str, ann):
            """Resolve a Query/Header/Path/Cookie marker from the
            current WS scope. Coerces via ``ann`` (the dep param's
            annotation). Raises ``WebSocketException(1008)`` when
            the marker is required and the value is absent."""
            kind = _marker_kind(marker)
            alias = getattr(marker, "alias", None) or alias_or_name
            raw = None
            if kind == "query":
                raw = _ws_qp.get(alias)
            elif kind == "header":
                # Header alias convert_underscores semantics: turn
                # ``user_agent`` → ``user-agent``.
                hdr_name = alias
                if isinstance(hdr_name, str):
                    hdr_name = hdr_name.replace("_", "-")
                raw = _ws_headers.get(hdr_name)
            elif kind == "path":
                raw = path_params.get(alias)
            elif kind == "cookie":
                # Parse cookies from the Host header.
                cookie_hdr = _ws_headers.get("cookie", "") or ""
                from http.cookies import SimpleCookie
                jar = SimpleCookie()
                try:
                    jar.load(cookie_hdr)
                except Exception:  # noqa: BLE001
                    pass
                morsel = jar.get(alias)
                raw = morsel.value if morsel is not None else None
            if raw is None:
                if _marker_is_required(marker):
                    raise _ws_required_missing(alias_or_name)
                # Use marker's default (if any).
                d = getattr(marker, "default", None)
                if d is ... or d is _insp_ws.Parameter.empty:
                    return None
                return d
            return _coerce_to(ann, raw)

        ws_dep_teardowns: list = []  # (gen, is_async, scope)

        async def _ws_resolve_dep(dep_callable, dep_cache, use_cache=True, dep_scope=None):
            """Recursive ``Depends`` resolver for the WS path.

            Handles per-param resolution the way upstream FastAPI
            does for WS dependencies:

              * ``Depends(other)`` → recurse.
              * ``Query(...)`` / ``Header(...)`` / ``Path(...)`` /
                ``Cookie(...)`` markers → pull from the matching
                scope, coerce via the dep's annotation, raise
                ``WebSocketException(1008)`` when required and
                missing.
              * ``WebSocket`` annotation → inject the connection.
              * Bare param with a name that's a path param → inject
                that path param (coerced).
              * Bare scalar with no marker → fall back to query
                string lookup (Starlette WS-dep convention).
              * Bare param with a default → use the default verbatim.

            Honour ``app.dependency_overrides`` first so WS deps
            obey the same override contract as HTTP deps.

            ``use_cache`` mirrors ``Depends(..., use_cache=...)``:
            when False, the resolver re-runs the dep on every
            usage within one WS session. Earlier this resolver
            unconditionally consulted ``dep_cache``, so two
            ``Depends(d, use_cache=False)`` params in one handler
            both got the FIRST call's value — broke FA's
            ``no-cache`` contract (probe-confirmed: turbo returned
            ``a=1, b=1, calls=1`` where upstream returns
            ``a=1, b=2, calls=2``).
            """
            _ws_overrides = getattr(self, "dependency_overrides", None) or {}
            _ws_orig_callable = dep_callable
            dep_callable = _ws_overrides.get(dep_callable, dep_callable)
            _scope_norm = (dep_scope or "request").lower()
            _cache_key_ws = (_ws_orig_callable, _scope_norm)
            if use_cache and _cache_key_ws in dep_cache:
                return dep_cache[_cache_key_ws]
            try:
                dep_sig = _insp_ws.signature(dep_callable)
            except (TypeError, ValueError):
                dep_sig = None
            dep_kwargs: dict = {}
            if dep_sig is not None:
                # Look up Depends() markers stashed in
                # ``Annotated[T, Depends(...)]`` metadata as well as
                # as the parameter ``default``. Without this, an
                # ``Annotated[Session, Depends(dep_session, scope=
                # 'request')]`` param would fall through to "treat
                # as path/query" and 422 the WS handshake.
                import typing as _tp_ws_local
                try:
                    _dep_hints = _tp_ws_local.get_type_hints(
                        dep_callable, include_extras=True,
                    )
                except Exception:  # noqa: BLE001
                    _dep_hints = {}
                for dpname, dp in dep_sig.parameters.items():
                    default = dp.default
                    ann = dp.annotation
                    # Pull from Annotated metadata if no default-form
                    # marker was supplied.
                    _ann_marker = None
                    _hint = _dep_hints.get(dpname)
                    if _hint is not None and hasattr(_hint, "__metadata__"):
                        for _m in getattr(_hint, "__metadata__", ()):
                            if isinstance(_m, _Dep_marker_ws):
                                _ann_marker = _m
                                break
                    if _ann_marker is not None and not isinstance(default, _Dep_marker_ws):
                        default = _ann_marker
                    # Nested Depends.
                    if isinstance(default, _Dep_marker_ws):
                        nested = default.dependency
                        if nested is not None:
                            dep_kwargs[dpname] = await _ws_resolve_dep(
                                nested,
                                dep_cache,
                                use_cache=getattr(default, "use_cache", True),
                                dep_scope=getattr(default, "scope", None),
                            )
                        continue
                    # Query / Header / Path / Cookie markers — coerce
                    # AND raise 1008 if required and missing.
                    if isinstance(default, _PM_ws):
                        dep_kwargs[dpname] = _resolve_marker_value(
                            default, dpname, ann
                        )
                        continue
                    # WebSocket injection.
                    if isinstance(ann, type) and issubclass(ann, _WS_cls):
                        dep_kwargs[dpname] = ws_obj
                        continue
                    # Bare path-param shorthand.
                    if dpname in path_params:
                        dep_kwargs[dpname] = _coerce_to(ann, path_params[dpname])
                        continue
                    # Bare scalar → query lookup with coercion.
                    if dpname in _ws_qp:
                        dep_kwargs[dpname] = _coerce_to(ann, _ws_qp[dpname])
                        continue
                    if default is not _insp_ws.Parameter.empty:
                        dep_kwargs[dpname] = default
                        continue
                    # No source for this param. Treat as required and
                    # close the socket — better to surface than to
                    # let the dep run with a missing kwarg (TypeError).
                    raise _ws_required_missing(dpname)
            # Handle generator / async-generator yield-deps so the
            # FA contract works: dep yields the value, then the
            # teardown runs after the WS handler completes (request
            # scope) or before it returns (function scope, FA
            # 0.120+).
            # Use the resolver-arg ``dep_scope`` (passed by callers).
            _scope_for_gen_ws = (dep_scope or "request").lower()
            if _insp_ws.isasyncgenfunction(dep_callable):
                _gen = dep_callable(**dep_kwargs)
                val = await _gen.__anext__()
                ws_dep_teardowns.append((_gen, True, _scope_for_gen_ws))
            elif _insp_ws.isgeneratorfunction(dep_callable):
                _gen = dep_callable(**dep_kwargs)
                val = next(_gen)
                ws_dep_teardowns.append((_gen, False, _scope_for_gen_ws))
            elif _insp_ws.iscoroutinefunction(dep_callable):
                val = await dep_callable(**dep_kwargs)
            else:
                val = dep_callable(**dep_kwargs)
                if _insp_ws.iscoroutine(val):
                    val = await val
                elif _insp_ws.isasyncgen(val):
                    _gen = val
                    val = await _gen.__anext__()
                    ws_dep_teardowns.append((_gen, True, _scope_for_gen_ws))
                elif _insp_ws.isgenerator(val):
                    _gen = val
                    val = next(_gen)
                    ws_dep_teardowns.append((_gen, False, _scope_for_gen_ws))
            # Cache under the ORIGINAL callable so subsequent
            # ``Depends(orig_callable)`` requests in the same WS
            # session hit the cache regardless of any
            # dependency_overrides indirection. Only store when
            # the caller asked us to cache — ``use_cache=False``
            # callers want a fresh value next time. Cache key
            # includes ``dep_scope`` to keep function/request copies
            # of the same callable separate (FA 0.120+).
            if use_cache:
                dep_cache[(_ws_orig_callable, _scope_for_gen_ws)] = val
            return val

        try:
            # Param resolution + endpoint call live in the SAME try
            # block so a ``Depends(auth)`` that raises
            # ``WebSocketException(1008)`` is caught by the WSE
            # handler below and closes the socket with the user's
            # code — instead of escaping the dispatcher entirely
            # (or worse, being silently swallowed). Same for
            # ``ValidationError`` from a typed path/query coercion:
            # bad-type input closes with 1008 rather than passing a
            # raw string where the user expects an int.
            ws_dep_cache: dict = {}
            # Run app/router/route-level extra dependencies BEFORE
            # the handler params resolve. Their return values are
            # discarded (FA contract — extra deps are for side
            # effects: auth, audit, append-to-list). Probe-confirmed
            # against ``test_ws_dependencies::test_index`` etc.
            _ws_extra_deps: list = []
            _ws_extra_deps.extend(getattr(self, "dependencies", []) or [])
            _ws_extra_deps.extend(getattr(self.router, "dependencies", []) or [])
            # FA's order: app → include-time deps (passed via
            # ``app.include_router(router, dependencies=[...])``) →
            # router's own deps → route's own deps. ``include_deps``
            # is stamped by ``include_router`` from the kwargs.
            _ws_extra_deps.extend(
                getattr(matched_route, "_fastapi_turbo_include_deps", []) or []
            )
            _ws_owner_router = getattr(
                matched_route, "_fastapi_turbo_owner_router", None
            )
            if _ws_owner_router is not None:
                _ws_extra_deps.extend(
                    getattr(_ws_owner_router, "dependencies", []) or []
                )
            _ws_extra_deps.extend(
                getattr(matched_route, "dependencies", []) or []
            )
            from fastapi_turbo.dependencies import Depends as _Dep_marker_ws_extra
            for _xd in _ws_extra_deps:
                if isinstance(_xd, _Dep_marker_ws_extra):
                    _xd_call = _xd.dependency
                    if _xd_call is None:
                        continue
                    await _ws_resolve_dep(
                        _xd_call,
                        ws_dep_cache,
                        use_cache=getattr(_xd, "use_cache", True),
                        dep_scope=getattr(_xd, "scope", None),
                    )

            for p in ws_introspect_params:
                pname = p.get("name")
                kind = p.get("kind")
                if pname == ws_param_name:
                    continue
                if kind == "dependency":
                    dep_callable = p.get("dep_callable")
                    if dep_callable is not None:
                        kwargs[pname] = await _ws_resolve_dep(
                            dep_callable,
                            ws_dep_cache,
                            use_cache=p.get("use_cache", True),
                            dep_scope=p.get("_dep_scope"),
                        )
                    continue
                if kind == "path":
                    if pname in path_params:
                        kwargs[pname] = _ws_coerce(p, path_params[pname])
                    continue
                if kind == "query":
                    alias = p.get("alias") or pname
                    if alias in _ws_qp:
                        kwargs[pname] = _ws_coerce(p, _ws_qp[alias])
                    elif p.get("required", False):
                        # Required ``Query(...)`` with no value:
                        # close 1008 instead of passing the marker
                        # object through as the kwarg (which would
                        # let the endpoint run with the marker as
                        # the value — a real auth bypass for
                        # ``token: str = Query(...)`` patterns).
                        raise _ws_required_missing(alias)
                    elif p.get("has_default", False):
                        kwargs[pname] = p.get("default_value")
                    continue
                if kind == "header":
                    alias = p.get("alias") or pname
                    if isinstance(alias, str):
                        alias = alias.replace("_", "-")
                    val = _ws_headers.get(alias)
                    if val is not None:
                        kwargs[pname] = _ws_coerce(p, val)
                    elif p.get("required", False):
                        raise _ws_required_missing(alias)
                    elif p.get("has_default", False):
                        kwargs[pname] = p.get("default_value")
                    continue
                # Path params not surfaced by introspection (e.g.
                # when the param has no marker but appears in the
                # URL template) still need to land on kwargs.
                if pname in path_params and pname not in kwargs:
                    kwargs[pname] = path_params[pname]

            # Last-mile: any signature param still unbound that
            # appears in path_params should land on kwargs (covers
            # users who name a path param without annotating it).
            for pname in sig.parameters:
                if pname in kwargs:
                    continue
                if pname in path_params:
                    kwargs[pname] = path_params[pname]

            if _insp_ws.iscoroutinefunction(endpoint):
                await endpoint(**kwargs)
            else:
                result = endpoint(**kwargs)
                if _insp_ws.iscoroutine(result):
                    await result
            # Drain ALL dep teardowns (function-scope first, then
            # request-scope — same order as HTTP, LIFO within each).
            # WS doesn't have a "post-response" phase distinct from
            # the handler returning, so all teardowns flush here.
            # The cache key already separates function/request copies
            # of the same callable, so the handler reads correct
            # ``func_is_open`` / ``req_is_open`` snapshots BEFORE
            # this drain. Errors propagate to the WS dispatcher's
            # outer envelope so the test client surfaces them
            # (matches FA's
            # ``test_websocket_dependency_after_yield_broken``).
            _ws_post_teardown_exc: BaseException | None = None
            for _g, _is_a, _sc in reversed(ws_dep_teardowns):
                try:
                    if _is_a:
                        try:
                            await _g.__anext__()
                        except StopAsyncIteration:
                            pass
                    else:
                        try:
                            next(_g)
                        except StopIteration:
                            pass
                except BaseException as _exc:  # noqa: BLE001
                    if _ws_post_teardown_exc is None:
                        _ws_post_teardown_exc = _exc
            ws_dep_teardowns.clear()
            if _ws_post_teardown_exc is not None:
                raise _ws_post_teardown_exc
        except _WSE as _wsex:
            # Endpoint raised ``WebSocketException(code=…)`` — Starlette
            # closes the WS with the user's code rather than the
            # generic 1011. This is what FA tests assert on
            # (``assert exc_info.value.code == 1008``).
            if not ws_obj._closed:
                try:
                    await ws_obj.close(
                        code=_wsex.code,
                        reason=getattr(_wsex, "reason", "") or "",
                    )
                except Exception:  # noqa: BLE001
                    pass
        except _WSD as _wsd:
            # Disconnects propagate from receive helpers when the
            # client closes mid-handler. Starlette stores this on
            # the task so the test client can surface it via
            # ``pytest.raises(WebSocketDisconnect)`` — match that
            # contract by re-raising. Probe-confirmed against
            # ``test_tutorial/test_websockets/test_tutorial002``.
            raise
        except _WS_PyVE as _vex:
            # Bad input type for a path / query / header (e.g.
            # ``room: int`` with ``/ws/abc``). Close with 1008
            # (policy violation) — closer to FA's 422 semantic than
            # the generic 1011 "internal error" which the client
            # would interpret as a server-side bug.
            _log.debug("in-process WS coercion failed: %r", _vex)
            if not ws_obj._closed:
                try:
                    await ws_obj.close(code=1008)
                except Exception:  # noqa: BLE001
                    pass
        except Exception as _exc:  # noqa: BLE001
            _log.debug("in-process WS endpoint raised: %r", _exc)
            # Honour ``app.exception_handlers`` for the matched
            # exception type. The handler signature is ``(websocket,
            # exc)`` — same as Starlette. The handler typically calls
            # ``websocket.close(code, reason)`` with a custom code,
            # which our test client surfaces to ``pytest.raises``.
            # Probe-confirmed against
            # ``test_ws_router::test_depend_err_handler``.
            _ws_handler, _ws_matched_cls = _find_exception_handler(self, _exc)
            if _ws_handler is not None and _ws_matched_cls is not Exception:
                try:
                    if _insp_ws.iscoroutinefunction(_ws_handler):
                        await _ws_handler(ws_obj, _exc)
                    else:
                        _r = _ws_handler(ws_obj, _exc)
                        if _insp_ws.iscoroutine(_r):
                            await _r
                except Exception:  # noqa: BLE001
                    pass
                # Drain teardowns and return — the handler took care
                # of closing the WS.
                for _g2, _is_a2, _sc2 in reversed(ws_dep_teardowns):
                    try:
                        if _is_a2:
                            try:
                                await _g2.__anext__()
                            except StopAsyncIteration:
                                pass
                        else:
                            try:
                                next(_g2)
                            except StopIteration:
                                pass
                    except Exception:  # noqa: BLE001
                        pass
                ws_dep_teardowns.clear()
                return True
            if not ws_obj._closed:
                try:
                    await ws_obj.close(code=1011)
                except Exception:  # noqa: BLE001
                    pass
            # Surface unhandled server-side exceptions to the test
            # client (FA's contract — ``raise_server_exceptions=
            # True`` propagates). The TestClient's ``__exit__`` on
            # the WS session re-raises any task exception caught
            # here. Probe-confirmed against
            # ``test_dependency_after_yield_websockets::test_websocket
            # _dependency_after_yield_broken``.
            try:
                _raise_se = getattr(self, "_fastapi_turbo_raise_server_exceptions", True)
            except Exception:  # noqa: BLE001
                _raise_se = True
            # Drain teardowns BEFORE re-raising so dep finalisers
            # run cleanly first.
            for _g2, _is_a2, _sc2 in reversed(ws_dep_teardowns):
                try:
                    if _is_a2:
                        try:
                            await _g2.__anext__()
                        except StopAsyncIteration:
                            pass
                    else:
                        try:
                            next(_g2)
                        except StopIteration:
                            pass
                except Exception:  # noqa: BLE001
                    pass
            ws_dep_teardowns.clear()
            if _raise_se:
                raise
        # Drain any teardowns left over after error paths so dep
        # finalisers run on every exit (including WS close
        # failures). Mirrors the HTTP post-response drain.
        for _g, _is_a, _sc in reversed(ws_dep_teardowns):
            try:
                if _is_a:
                    try:
                        await _g.__anext__()
                    except StopAsyncIteration:
                        pass
                else:
                    try:
                        next(_g)
                    except StopIteration:
                        pass
            except Exception:  # noqa: BLE001
                pass
        ws_dep_teardowns.clear()
        return True

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
            f"fastapi-turbo ASGI adapter: Rust server did not start on port {port} "
            f"within {timeout}s"
        )

    # ── HTTP proxy ────────────────────────────────────────────────────

    async def _asgi_proxy_http(self, scope: dict, receive: Callable, send: Callable) -> None:
        """Dispatch an ASGI HTTP request.

        This is what runs when something ``await``s our app as an ASGI
        callable — e.g. ``httpx.AsyncClient(transport=ASGITransport(app))``
        or ``uvicorn myapp:app`` (without our own Rust server).

        Two dispatch paths:

          1. If the scope carries an ``x-fastapi-turbo-dispatch: inproc``
             marker (set by our in-process adapter), OR if the Rust
             server failed to bind, we run the matched route's Python
             endpoint directly via ``_dispatch_to_subapp_route`` (the
             same helper ``app.host()`` uses). This is the path that
             works in socket-restricted environments.

          2. Otherwise we proxy the request over localhost to our Rust
             server. This preserves the full Tower middleware stack +
             Axum routing semantics but needs a working loopback.

        Header handling is now duplicate-safe: we rebuild a
        ``httpx.Headers`` from the ASGI ``(name_bytes, value_bytes)``
        tuple list so repeated Set-Cookie / X-Forwarded-For values
        survive round-trip.
        """
        import httpx

        # Reconstruct the URL
        path = scope.get("path", "/")
        qs = scope.get("query_string", b"")
        url = f"http://127.0.0.1:{self._asgi_server_port}{path}"
        if qs:
            url += f"?{qs.decode('latin-1')}"

        # Reconstruct headers as a list of (name, value) pairs so
        # duplicate headers (X-Forwarded-For, Set-Cookie on the
        # request side, etc.) aren't silently collapsed. httpx accepts
        # either a dict or a list of tuples.
        headers_list = scope.get("headers", [])
        headers: list[tuple[str, str]] = []
        for name_bytes, value_bytes in headers_list:
            name = name_bytes.decode("latin-1") if isinstance(name_bytes, bytes) else name_bytes
            value = value_bytes.decode("latin-1") if isinstance(value_bytes, bytes) else value_bytes
            # Skip hop-by-hop headers — httpx / the server will recompute.
            if name.lower() in ("host", "transfer-encoding", "connection"):
                continue
            headers.append((name, value))

        # Stream the request body via an async iterator so large
        # uploads aren't fully buffered in memory before hand-off to
        # the Rust server. httpx accepts an async iterable via
        # ``content=``.
        async def _body_iter():
            while True:
                message = await receive()
                chunk = message.get("body", b"")
                if chunk:
                    yield chunk
                if not message.get("more_body", False):
                    return

        method = scope.get("method", "GET")

        async with httpx.AsyncClient() as client:
            resp = await client.request(
                method=method,
                url=url,
                headers=headers,
                content=_body_iter(),
                follow_redirects=False,
            )

        # Response headers as list-of-tuples via ``multi_items`` so
        # duplicate Set-Cookie values reach the ASGI caller intact.
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
