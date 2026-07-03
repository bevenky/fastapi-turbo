"""The main FastAPI-compatible application class."""

from __future__ import annotations

import asyncio
import atexit
import inspect
import json
import logging
import os
from typing import Any, Callable, Sequence

# Real pip FastAPI for the "accelerate real FastAPI" pivot. This module is imported
# by fastapi_turbo/__init__.py BEFORE ``compat.install()`` patches the accelerated
# entry points onto the real package (see __init__.py), so the class statement
# below subclasses the GENUINE ``fastapi.FastAPI``.
import fastapi as _real_fastapi

# NOTE on patched-name lookups: the patch-on-real compat installer
# setattr-patches ``fastapi.routing.APIRoute`` (and friends) to the turbo
# subclasses on this SAME live module object, so a runtime lookup through
# ``_real_fastapi.routing.APIRoute`` returns the turbo subclass once the
# patcher has run. That is deliberately what the internal real-route
# construction sites below use: real ``get_openapi`` (and real
# ``include_router``) isinstance-check through the SAME live attribute, so
# constructing with the live class keeps checker and instances consistent
# both pre- and post-install. Only resolve through ``_real_fastapi`` paths
# the installer never patches (``openapi.utils.get_openapi``,
# ``datastructures.Default``, ``responses.JSONResponse``, ...) when the
# genuine object is required.

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


from fastapi_turbo._door_support import _make_sync_wrapper
from fastapi_turbo.datastructures import State
from fastapi_turbo.routing import (
    APIRouter,
    APIRoute,
    _looks_like_starlette_mount,
    _looks_like_starlette_websocket_route,
    _mark_starlette_compat_route,
    _unset_to_none,
)
from fastapi_turbo._ws_support import (
    _adapt_websocket_endpoint_class,
    _wrap_websocket_endpoint,
)


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
    _build_custom_route_handler_endpoint,
    _close_one_upload,
    _close_upload_files,
    _has_overridden_get_route_handler,
    _is_async_callable,
    _model_needs_full_dump,
)


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
from fastapi_turbo.responses import Response as _real_starlette_response


# Middleware-wrap machinery extracted to ``_middleware_wrap.py``.
from fastapi_turbo._middleware_wrap import (  # noqa: F401 — public-shape re-exports
    _drive_async_fallback,
    _make_asgi_middleware_shim,
    _MiddlewareSuspendedError,
    _wrap_with_http_middlewares,
)


def _door_wrap_stream_teardown(app, stream_response, req_gens):
    """Defer REQUEST-scope yield-dependency teardown to AFTER the streaming body is
    fully sent (FastAPI ``request_stack`` order). The door owns the response, so it
    wraps the ``StreamingResponse``'s ``body_iterator`` here; the teardown logic
    itself stays in Python — drive each generator past its yield (LIFO), capturing
    a post-yield raise onto the app since the response is already streaming."""
    inner = stream_response.body_iterator

    # Propagate the no-await verdict of the REAL user gen (``inner``) onto the
    # response BEFORE wrapping. The async ``_wrapped`` below only does
    # ``async for ... : yield`` (no ``GET_AWAITABLE`` of its own), so bytecode
    # analysis of the wrapper would wrongly green-light the Rust inline-drive
    # fast path even when ``inner`` awaits. Stamping the wrapped gen's verdict
    # here keeps that decision keyed on the real body. (Only meaningful for
    # async ``inner``; sync ``inner`` doesn't reach the async fast path.)
    if hasattr(inner, "__aiter__"):
        try:
            from fastapi_turbo.responses import _gen_is_noawait as _gina

            stream_response._fastapi_turbo_stream_noawait = _gina(inner)
        except Exception:  # noqa: BLE001
            stream_response._fastapi_turbo_stream_noawait = False
        # The wrapper below shares ONE code object across every wrapped route,
        # so the Rust runtime-cooperative classification (trampoline vs worker
        # loop, streaming.rs) must key on the REAL user gen's code — stamp it
        # alongside the no-await verdict.
        stream_response._fastapi_turbo_stream_code = getattr(
            inner, "ag_code", None
        )

    def _teardown():
        for g in reversed(req_gens):
            try:
                g.send(None)
            except StopIteration:
                pass
            except BaseException as exc:  # noqa: BLE001
                if app is not None:
                    try:
                        app._captured_server_exceptions.append(exc)
                    except Exception:  # noqa: BLE001
                        pass
            else:
                try:
                    g.close()
                except BaseException:  # noqa: BLE001
                    pass

    if hasattr(inner, "__aiter__"):

        async def _wrapped():
            try:
                async for chunk in inner:
                    yield chunk
            finally:
                _teardown()

    else:

        def _wrapped():
            try:
                yield from inner
            finally:
                _teardown()

    stream_response.body_iterator = _wrapped()


def _load_real_starlette_class(submodule: str, classname: str):
    """Load the GENUINE Starlette class for the in-process dispatcher.

    Post shim-flip, ``starlette.*`` modules in ``sys.modules`` ARE the real
    package — but the compat patcher rebinds a few attributes (the
    Tower-marker middleware classes among them) to turbo stand-ins that are
    inert as ASGI. When the in-process / TestClient path needs the real
    implementation (so CORS / GZip / HTTPSRedirect actually run), consult
    the patcher's saved original for that exact (module, attribute) first;
    fall back to the live attribute when it was never patched.

    This replaces the pre-flip snapshot/evict/re-import dance (which dodged
    the fake sys.modules entries at the cost of a process-wide lock and
    duplicate class identities from the fresh import)."""
    import importlib

    try:
        mod = importlib.import_module(f"starlette.{submodule}")
    except Exception:  # noqa: BLE001
        return None
    try:
        from fastapi_turbo import compat as _compat

        for patched_mod, attr, original in _compat._PATCHES:
            if patched_mod is mod and attr == classname:
                # First record for this (module, attr) holds the genuine
                # pre-patch value.
                return None if original is _compat._MISSING else original
    except Exception as _exc:  # noqa: BLE001
        _log.debug("compat original lookup failed: %r", _exc)
    return getattr(mod, classname, None)


# Real Starlette/FastAPI middleware class names → fastapi-turbo
# middleware-type tag. Post-flip, ``app.add_middleware(...)`` is handed the
# REAL middleware class, which lacks our ``_fastapi_turbo_middleware_type``
# marker — so we resolve it by class name (distinctive across the Starlette
# middleware suite) and match across the MRO so user subclasses resolve too.
# Pre-flip these names ALSO match the clone classes, but the marker (checked
# first in ``_tower_type_for``) wins there, keeping behaviour identical.
_REAL_MW_NAME_TO_TYPE = {
    "CORSMiddleware": "cors",
    "GZipMiddleware": "gzip",
    "HTTPSRedirectMiddleware": "httpsredirect",
    "TrustedHostMiddleware": "trustedhost",
    "SessionMiddleware": "python_http_session",
    "AuthenticationMiddleware": "python_http_auth",
    "BaseHTTPMiddleware": "base_http",
}


def _tower_type_for(mw_cls):
    """Resolve a middleware class (or string shorthand) to its
    fastapi-turbo middleware-type tag, or ``None`` if unknown.

    Recognises BOTH the clone's ``_fastapi_turbo_middleware_type`` marker
    (which wins when present) AND real Starlette middleware classes by name,
    so after the shim flip ``app.add_middleware(CORSMiddleware, ...)`` with
    the *real* class still maps to the right Tower layer / Python-HTTP path.
    A string is returned as-is (``app.add_middleware('cors', ...)``)."""
    if isinstance(mw_cls, str):
        return mw_cls
    marker = getattr(mw_cls, "_fastapi_turbo_middleware_type", None)
    if marker:
        return marker
    for base in getattr(mw_cls, "__mro__", ()):
        tag = _REAL_MW_NAME_TO_TYPE.get(getattr(base, "__name__", ""))
        if tag:
            return tag
    return None


def _resolve_tower_bound_to_asgi_class(mw_cls):
    """Map a Tower-bound middleware marker class to its real
    Starlette ASGI3 equivalent so the in-process dispatcher can
    apply it like any other middleware. The Tower path uses these
    markers as routing flags only — they're inert as ASGI on their
    own. For the in-process / TestClient path we substitute the
    real Starlette class loaded around the shim.

    Accepts class markers, real Starlette classes (resolved by name via
    ``_tower_type_for``), AND string-shorthand forms — ``app.add_middleware(
    'cors', ...)`` registers the string directly so we look it up here too.

    Returns ``None`` if the class / string isn't a Tower-bound marker
    we know how to substitute (only CORS/GZip/HTTPSRedirect substitute —
    TrustedHost runs through the raw-ASGI chain unchanged)."""
    mw_type = _tower_type_for(mw_cls)
    if mw_type == "cors":
        return _load_real_starlette_class("middleware.cors", "CORSMiddleware")
    if mw_type == "gzip":
        return _load_real_starlette_class("middleware.gzip", "GZipMiddleware")
    if mw_type == "httpsredirect":
        return _load_real_starlette_class(
            "middleware.httpsredirect", "HTTPSRedirectMiddleware"
        )
    return None


def _request_injection_param(
    name: str = "request", *, handler_param: bool = True
) -> dict[str, Any]:
    return {
        "name": name,
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
        "_is_handler_param": handler_param,
    }


def _response_from_asgi_messages(
    status_code: int, headers: list, body_parts: list[bytes]
):
    from fastapi_turbo.responses import Response as _Response

    resp = _Response(content=b"".join(body_parts), status_code=status_code)
    # ``raw_headers.clear()`` resets the list the MutableHeaders view is bound
    # to (real Starlette has no ``headers.clear()``); the append below
    # repopulates from the inner app's headers.
    resp.raw_headers.clear()
    for raw_k, raw_v in headers:
        k = raw_k.decode("latin-1") if isinstance(raw_k, bytes) else str(raw_k)
        v = raw_v.decode("latin-1") if isinstance(raw_v, bytes) else str(raw_v)
        resp.headers.append(k, v)
    return resp


async def _run_asgi_http_app_to_response(asgi_app, request):
    scope = dict(getattr(request, "scope", {}) or {})
    scope["type"] = "http"
    body_bytes = await request.body() if hasattr(request, "body") else b""
    sent_body = False

    async def _receive():
        nonlocal sent_body
        if sent_body:
            return {"type": "http.disconnect"}
        sent_body = True
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
                body_parts.append(bytes(chunk))

    await asgi_app(scope, _receive, _send)
    return _response_from_asgi_messages(
        int(status_holder["status"]),
        list(status_holder.get("headers") or []),
        body_parts,
    )


def _is_http_endpoint_class(endpoint) -> bool:
    if not isinstance(endpoint, type):
        return False
    return any(base.__name__ == "HTTPEndpoint" for base in endpoint.__mro__)


async def _run_starlette_http_endpoint(endpoint, request):
    if _is_http_endpoint_class(endpoint):
        return await _run_asgi_http_app_to_response(endpoint, request)
    result = endpoint(request)
    if inspect.isawaitable(result):
        result = await result
    return result


def _mounted_route_asgi_app(app_cls, route):
    mounted_app = getattr(route, "app", None)
    if mounted_app is None and getattr(route, "routes", None):
        mounted_app = app_cls(
            routes=list(getattr(route, "routes") or []),
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )
    return mounted_app


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
    # Raw model / dataclass / scalar — jsonable_encoder first (FA's
    # default-return contract). Real Starlette JSONResponse passes no
    # ``default=`` and would raise on a raw BaseModel.
    from fastapi_turbo.encoders import jsonable_encoder as _je
    return _JR(content=_je(result))


def _wrap_with_exception_handlers(handler, app):
    """Wrap a handler so exceptions it raises are dispatched to the app's CUSTOM
    exception handlers — mirroring the clone's compiled handler. Without this, the
    adapter's raw handler would hit the door's default handling instead of the
    user's ``@app.exception_handler``. Returns the handler unchanged when the app
    has no custom handlers (the door's defaults already apply)."""
    if not getattr(app, "exception_handlers", None):
        return handler

    def wrapped(**kwargs):
        try:
            return handler(**kwargs)
        except Exception as exc:  # noqa: BLE001
            result = None
            raised = False
            try:
                result = app._invoke_exception_handler(exc)
            except Exception:  # noqa: BLE001
                raised = True
            # Starlette parity bookkeeping: capture non-HTTPException exceptions
            # not handled by a SPECIFIC handler (the ``Exception`` catch-all still
            # re-raises) for raise_server_exceptions / Sentry.
            handled_specific = False
            if result is not None and not raised:
                for exc_cls in app.exception_handlers:
                    if exc_cls is Exception:
                        continue
                    if isinstance(exc_cls, type) and isinstance(exc, exc_cls):
                        handled_specific = True
                        break
            try:
                from fastapi_turbo.exceptions import HTTPException as _HE

                if not isinstance(exc, _HE) and not handled_specific:
                    captured = getattr(app, "_captured_server_exceptions", None)
                    if captured is not None:
                        captured.append(exc)
            except Exception:  # noqa: BLE001
                pass
            if result is not None and not raised:
                return result
            raise

    wrapped.__name__ = getattr(handler, "__name__", "handler")
    return wrapped


def _async_inline_enabled() -> bool:
    """``FASTAPI_TURBO_ASYNC_INLINE=1``: register eligible coroutine handlers
    as genuinely async (``is_async=True``) so the Rust door drives the request
    on the persistent worker loop end-to-end (router.rs async-inline path)
    instead of wrapping them in the SYNC submit-caller (which blocks a tokio
    thread on a ``threading.Event`` for the whole request)."""
    import os

    return os.environ.get("FASTAPI_TURBO_ASYNC_INLINE", "").strip() in (
        "1",
        "true",
        "True",
        "TRUE",
        "yes",
        "on",
    )


def _wrap_with_exception_handlers_async(handler, app):
    """Async twin of ``_wrap_with_exception_handlers``: same custom-handler
    dispatch + capture bookkeeping, but awaits the coroutine INSIDE the wrapper
    so handler-raised exceptions (raised during await, not at coroutine
    creation) are actually caught. Returns the handler unchanged when the app
    has no custom handlers. Used by the async-inline registration path, which
    must keep the handler a genuine coroutine function."""
    if not getattr(app, "exception_handlers", None):
        return handler

    async def wrapped(**kwargs):
        try:
            return await handler(**kwargs)
        except Exception as exc:  # noqa: BLE001
            result = None
            raised = False
            try:
                result = await app._ainvoke_exception_handler(exc)
            except Exception:  # noqa: BLE001
                raised = True
            handled_specific = False
            if result is not None and not raised:
                for exc_cls in app.exception_handlers:
                    if exc_cls is Exception:
                        continue
                    if isinstance(exc_cls, type) and isinstance(exc, exc_cls):
                        handled_specific = True
                        break
            try:
                from fastapi_turbo.exceptions import HTTPException as _HE

                if not isinstance(exc, _HE) and not handled_specific:
                    captured = getattr(app, "_captured_server_exceptions", None)
                    if captured is not None:
                        captured.append(exc)
            except Exception:  # noqa: BLE001
                pass
            if result is not None and not raised:
                return result
            raise

    wrapped.__name__ = getattr(handler, "__name__", "handler")
    return wrapped


def _clone_framework_types() -> tuple:
    """Clone framework types whose presence in a handler signature forces the
    route onto the clone path. Request / HTTPConnection / BackgroundTasks /
    Response are real starlette (sub)classes that real get_dependant recognizes —
    and the door now shares ONE injected Response per request (handler + deps),
    so a ``response: Response`` handler param is handled on the adapter. Only
    UploadFile and WebSocket remain: UploadFile still has adapter edges (close
    lifecycle / Form+File), and a ``ws: WebSocket`` param only appears on WS
    routes (declined earlier via is_websocket; the entry also keeps the returned
    tuple non-empty so _signature_uses_clone_framework_type doesn't decline all)."""
    types = []
    for mod, name in (
        ("fastapi_turbo.websockets", "WebSocket"),
    ):
        try:
            types.append(getattr(__import__(mod, fromlist=[name]), name))
        except Exception:
            pass
    return tuple(types)


def _resolved_hints(endpoint) -> dict | None:
    """Resolved type hints (``include_extras=True``) for the endpoint, handling
    ``from __future__ import annotations`` (string annotations). Returns None if
    they can't be resolved — callers then conservatively decline."""
    import typing

    try:
        hints = dict(typing.get_type_hints(endpoint, include_extras=True))
        hints.pop("return", None)
        return hints
    except Exception:
        return None


def _signature_uses_clone_framework_type(endpoint) -> bool:
    """True if the endpoint signature annotates a param with a clone framework type
    (Request/Response/BackgroundTasks/UploadFile/WebSocket/HTTPConnection). Real
    FastAPI can't introspect those clone reimplementations, so such routes stay on
    the clone path (until the types are bridged to real starlette subclasses)."""
    import typing

    fw = _clone_framework_types()
    if not fw:
        return True
    hints = _resolved_hints(endpoint)
    if hints is None:
        return True

    def _bare(ann):
        if typing.get_origin(ann) is typing.Annotated:
            ann = typing.get_args(ann)[0]
        origin = typing.get_origin(ann)
        # Recurse through Union/Optional and container generics (list[UploadFile],
        # Optional[UploadFile], etc.) — any framework type anywhere disqualifies.
        if origin is not None:
            return any(
                a is not type(None) and _bare(a) for a in typing.get_args(ann)
            )
        return isinstance(ann, type) and issubclass(ann, fw)

    return any(_bare(h) for h in hints.values())


def _oa_stream_info(response_model, response_class, endpoint):
    """For the real-OpenAPI route build: classify a streaming endpoint. Returns
    ``(needs_response_model_none, is_sse, is_json, inner_model_or_None)``.

    - ``needs_response_model_none``: the return is ``AsyncIterable[...]`` / a
      generator that real ``get_dependant`` CAN'T field — build with
      ``response_model=None`` (true for SSE, NDJSON, and raw StreamingResponse).
    - ``is_sse`` (``response_class`` is ``EventSourceResponse``) / ``is_json``
      (NDJSON: NO response_class + AsyncIterable/generator): set real's native
      ``route.is_sse_stream`` / ``is_json_stream`` + ``stream_item_field`` AFTER the
      build so real get_openapi emits the SSE envelope / jsonl itemSchema.
    - A raw ``StreamingResponse`` (``response_class`` set, not SSE) needs
      ``response_model=None`` but NO content schema (matches upstream: 200 with only
      a description) — hence is_json is gated on ``response_class is None``."""
    import collections.abc as _abc
    import typing as _typing
    import inspect as _insp

    _ro = _typing.get_origin(response_model)
    needs_none = _ro in (
        _abc.AsyncIterable, _abc.AsyncIterator, _abc.AsyncGenerator,
        _abc.Iterable, _abc.Iterator, _abc.Generator,
    ) or _insp.isasyncgenfunction(endpoint) or _insp.isgeneratorfunction(endpoint)

    is_sse = False
    try:
        from fastapi_turbo.responses import EventSourceResponse as _ESR
        if isinstance(response_class, type) and issubclass(response_class, _ESR):
            is_sse = True
    except Exception:  # noqa: BLE001
        pass
    # NDJSON only when there's NO response_class (a raw StreamingResponse documents
    # no content). EventSourceResponse already handled by is_sse.
    is_json = (not is_sse) and (response_class is None) and bool(needs_none)

    inner = None
    if is_sse or is_json:
        _args = _typing.get_args(response_model)
        if _args:
            inner = _args[0]
        try:  # the SSE transport wrapper is not the content model
            from fastapi_turbo.sse import ServerSentEvent as _SSE
            if isinstance(inner, type) and issubclass(inner, _SSE):
                inner = None
        except Exception:  # noqa: BLE001
            pass
        if not (isinstance(inner, type) and hasattr(inner, "model_json_schema")):
            inner = None
    return (bool(needs_none) or is_sse, is_sse, is_json, inner)


def _oa_apply_stream(real_route, is_sse, is_json, inner) -> None:
    """Set the native streaming attrs on a real APIRoute (built with
    response_model=None) so real get_openapi emits the SSE/jsonl itemSchema."""
    if is_sse:
        real_route.is_sse_stream = True
    elif is_json:
        real_route.is_json_stream = True
    if inner is not None:
        real_route.stream_item_field = _real_fastapi.utils.create_model_field(
            name=getattr(inner, "__name__", "StreamItem"),
            type_=inner,
            mode="serialization",
        )


def _build_stream_handler(orig_endpoint, response_model, response_class, app):
    """Wrap a generator endpoint (sync OR async) into a SYNC handler returning a
    StreamingResponse — shared by the clone collection path AND the adapter so
    generator routes ride the fast adapter path (off the clone). With a custom
    ``response_class`` (EventSourceResponse / a StreamingResponse subclass) the
    generator object is wrapped via the class; otherwise it auto-wraps to an
    ``application/jsonl`` StreamingResponse (FA 0.136 native), validating each item
    against an ``AsyncIterable[Item]`` response_model and surfacing
    ``ResponseValidationError`` via ``app._captured_server_exceptions`` (TestClient
    re-raise parity)."""
    import inspect as _insp

    is_async_gen = _insp.isasyncgenfunction(orig_endpoint)
    # A custom response_class (a TYPE) wraps the generator object directly (SSE /
    # raw StreamingResponse). The framework default is a DefaultPlaceholder
    # INSTANCE (not a type) → fall through to the NDJSON auto-wrap.
    rc = response_class if isinstance(response_class, type) else None
    if rc is not None:
        # EventSourceResponse is a MARKER — real FastAPI does the SSE encoding in
        # the routing layer (here). Format each yielded item as an SSE event +
        # keepalive pings + the SSE headers, returning a plain StreamingResponse the
        # door streams. Mirrors fastapi/routing.py's is_sse_stream branch.
        _is_sse = False
        try:
            from fastapi_turbo.responses import EventSourceResponse as _ESR

            _is_sse = issubclass(rc, _ESR)
        except Exception:  # noqa: BLE001
            _is_sse = False
        if _is_sse:

            def _sse_wrap(_orig=orig_endpoint, _is_a=is_async_gen, **kwargs):
                from fastapi_turbo.responses import StreamingResponse as _SR
                from fastapi.encoders import jsonable_encoder as _je
                import fastapi_turbo.sse as _sse_mod
                import json as _json
                import anyio as _anyio
                from starlette.concurrency import iterate_in_threadpool

                _SSEv = _sse_mod.ServerSentEvent
                _fmt = _sse_mod.format_sse_event

                def _serialize(item):
                    if isinstance(item, _SSEv):
                        if item.raw_data is not None:
                            ds = item.raw_data
                        elif item.data is not None:
                            _mdj = getattr(item.data, "model_dump_json", None)
                            ds = _mdj() if callable(_mdj) else _json.dumps(_je(item.data))
                        else:
                            ds = None
                        return _fmt(
                            data_str=ds, event=item.event, id=item.id,
                            retry=item.retry, comment=item.comment,
                        )
                    return _fmt(data_str=_json.dumps(_je(item)))

                gen = _orig(**kwargs)
                sse_aiter = gen.__aiter__() if _is_a else iterate_in_threadpool(gen)

                # Keepalive: real FastAPI decouples iteration from the ping timer
                # with an anyio task group, but the door drives an async-gen with a
                # NEW task per __anext__, so a task group spanning the generator's
                # yields raises "exit cancel scope in a different task". Instead use
                # an asyncio producer task feeding a Queue + per-item asyncio.wait_for
                # — the timeout lives entirely within ONE __anext__ (no cross-task
                # scope), and a missing item just re-waits (queue keeps it).
                async def _stream():
                    import asyncio as _asyncio

                    _done = object()
                    q: _asyncio.Queue = _asyncio.Queue()

                    async def _producer():
                        err = None
                        try:
                            async for raw in sse_aiter:
                                await q.put((_serialize(raw), None))
                        except BaseException as exc:  # noqa: BLE001
                            err = exc
                        await q.put((_done, err))

                    # Ping interval is read where the tests monkeypatch it
                    # (``fastapi.routing._PING_INTERVAL``), with the sse default.
                    try:
                        import fastapi.routing as _fr

                        ping = getattr(_fr, "_PING_INTERVAL", _sse_mod._PING_INTERVAL)
                    except Exception:  # noqa: BLE001
                        ping = _sse_mod._PING_INTERVAL

                    prod = _asyncio.ensure_future(_producer())
                    try:
                        while True:
                            try:
                                item, err = await _asyncio.wait_for(q.get(), ping)
                            except (TimeoutError, _asyncio.TimeoutError):
                                yield _sse_mod.KEEPALIVE_COMMENT
                                continue
                            if item is _done:
                                if err is not None:
                                    raise err
                                break
                            yield item
                    finally:
                        prod.cancel()

                resp = _SR(_stream(), media_type="text/event-stream")
                resp.headers["Cache-Control"] = "no-cache"
                resp.headers["X-Accel-Buffering"] = "no"
                return resp

            return _sse_wrap

        def _rc_wrap(_orig=orig_endpoint, _rc=rc, **kwargs):
            return _rc(_orig(**kwargs))

        return _rc_wrap

    # NDJSON auto-wrap. Item validation when the return is ``AsyncIterable[Item]``.
    item_adapter = None
    import typing as _tp
    import collections.abc as _cabc

    if _tp.get_origin(response_model) in {
        _cabc.AsyncIterable, _cabc.AsyncIterator, _cabc.AsyncGenerator,
        _cabc.Iterable, _cabc.Iterator, _cabc.Generator,
    }:
        _args = _tp.get_args(response_model)
        if _args:
            try:
                from pydantic import TypeAdapter as _TA
                item_adapter = _TA(_args[0])
            except Exception as _exc:  # noqa: BLE001
                _log.debug("silent catch in applications: %r", _exc)
                item_adapter = None

    def _json_lines_wrap(
        _orig=orig_endpoint, _is_a=is_async_gen,
        _ta=item_adapter, _app=app, **kwargs,
    ):
        from fastapi_turbo.responses import StreamingResponse as _SR
        from fastapi_turbo.encoders import jsonable_encoder as _je
        from fastapi_turbo.exceptions import ResponseValidationError as _RVE
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
                        yield (_json.dumps(_je(validated), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
                except _RVE as exc:
                    if _app is not None:
                        _app._captured_server_exceptions.append(exc)
                    return

            return _SR(_iter_async(), media_type="application/jsonl")
        else:
            def _iter_sync():
                try:
                    for item in _orig(**kwargs):
                        validated = _check(item)
                        yield (_json.dumps(_je(validated), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
                except _RVE as exc:
                    if _app is not None:
                        _app._captured_server_exceptions.append(exc)
                    return

            return _SR(_iter_sync(), media_type="application/jsonl")

    return _json_lines_wrap


class FastAPI(_real_fastapi.FastAPI):
    """Drop-in replacement for ``fastapi.FastAPI``, backed by Rust Axum.

    Pivot: now SUBCLASSES real ``fastapi.FastAPI`` (which is a Starlette subclass).
    During the staged migration the clone's own attributes/methods (set in
    ``__init__`` and defined below) still shadow the real base; later steps delete
    those overrides so real FastAPI's routing/OpenAPI/dependency machinery and
    Starlette's ASGI app (via ``super().__call__()``) take over.
    """

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
        routes: Sequence | None = None,
        **kwargs: Any,
    ):
        # Initialize the real FastAPI base first (its routing/openapi/middleware/
        # lifespan machinery). The clone's own state set below currently shadows
        # it during the staged migration; later steps delete the clone overrides.
        super().__init__(
            title=title,
            summary=summary,
            description=description,
            version=version,
            openapi_url=openapi_url,
            docs_url=docs_url,
            redoc_url=redoc_url,
            root_path=root_path,
            debug=debug,
            lifespan=lifespan,
        )
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
        self._mounts: list[tuple[str, Any, str | None]] = []
        # FA / Starlette parity: ``FastAPI(routes=[...])`` registers
        # the supplied Starlette ``Route`` / ``WebSocketRoute`` /
        # ``Mount`` instances on the router so they're served like
        # any decorator-registered route. Earlier the kwarg was
        # accepted via ``**kwargs`` and silently dropped — the route
        # collection went nowhere and clients hit 404 (R52 finding 1).
        # Mark each as Starlette-passthrough so the in-process
        # dispatcher dispatches via Starlette ASGI semantics
        # (``await endpoint(request)``) instead of FastAPI's
        # parameter-injection introspection — the user's endpoint
        # is a ``(request) -> Response`` callable, not the
        # decorator-style ``def hi(item: Item): ...``.
        if routes:
            for _r in routes:
                _mark_starlette_compat_route(_r)
                self.router.routes.append(_r)

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
                args_m = tuple(getattr(m, "args", ()))
                kwargs_m = m.kwargs if hasattr(m, "kwargs") else (m[1] if len(m) > 1 else {})
                self.add_middleware(cls, *args_m, **kwargs_m)

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

    def route(
        self,
        path: str,
        methods: list[str] | None = None,
        name: str | None = None,
        include_in_schema: bool = True,
        **kwargs: Any,
    ):
        return self.router.route(
            path,
            methods=methods,
            name=name,
            include_in_schema=include_in_schema,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # WebSocket decorator
    # ------------------------------------------------------------------

    def websocket(self, path: str, name: str | None = None, **kwargs: Any):
        return self.router.websocket(path, name=name, **kwargs)

    # ------------------------------------------------------------------
    # Imperative route registration
    # ------------------------------------------------------------------

    def add_api_route(self, path: str, endpoint: Callable, **kwargs: Any) -> None:
        """Imperative form of @app.get / @app.post / etc."""
        return self.router.add_api_route(path, endpoint, **kwargs)

    def add_api_websocket_route(self, path: str, endpoint: Callable, **kwargs: Any) -> None:
        """Imperative form of @app.websocket."""
        return self.router.add_websocket_route(path, endpoint, **kwargs)

    def add_websocket_route(
        self,
        path: str,
        endpoint: Callable,
        name: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Starlette-compatible imperative WebSocket route registration."""
        return self.router.add_websocket_route(path, endpoint, name=name, **kwargs)

    def add_route(
        self,
        path: str,
        route: Callable,
        methods: list[str] | None = None,
        name: str | None = None,
        include_in_schema: bool = True,
        **kwargs: Any,
    ) -> None:
        """Starlette-compatible add_route."""
        return self.router.add_route(
            path,
            route,
            methods=methods,
            name=name,
            include_in_schema=include_in_schema,
            **kwargs,
        )

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

    def host(
        self,
        hostname: str | None = None,
        app: Any = None,
        name: str | None = None,
        **kwargs: Any,
    ) -> None:
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
        if hostname is None:
            hostname = kwargs.pop("host", None)
        if kwargs:
            unexpected = next(iter(kwargs))
            raise TypeError(
                f"FastAPI.host() got an unexpected keyword argument {unexpected!r}"
            )
        if hostname is None:
            raise TypeError("FastAPI.host() missing required argument: 'host'")
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

            # The shadow mirror exists ONLY for ``app.router.routes``
            # parity (callers iterating routes see sub-routes at their
            # final paths); the door's flatten walks
            # ``_included_routers`` + include_meta for the real
            # cascades. The old response-class / deps / owner-router
            # stamps on the clones were write-only — nothing read them.
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

    def add_middleware(self, middleware_cls, *args: Any, **kwargs: Any) -> None:
        """Register a middleware class. Delegates to the internal impl,
        then reorders so SentryAsgiMiddleware (if auto-installed) stays
        runtime-outermost regardless of add order."""
        try:
            self._add_middleware_impl(middleware_cls, *args, **kwargs)
        finally:
            self._keep_sentry_outermost()

    def _add_middleware_impl(self, middleware_cls, *args: Any, **kwargs: Any) -> None:
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

        mw_type = _tower_type_for(middleware_cls)
        if mw_type and mw_type.startswith("python_http_"):
            try:
                if args:
                    instance = middleware_cls(self, *args, **kwargs)
                else:
                    instance = middleware_cls(app=self, **kwargs)
            except TypeError:
                instance = middleware_cls(*args, **kwargs)
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
                if args:
                    instance = middleware_cls(self, *args, **kwargs)
                else:
                    instance = middleware_cls(app=self, **kwargs)
            except TypeError:
                instance = middleware_cls(*args, **kwargs)

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
                if args:
                    original_cls = middleware_cls

                    class _BoundMiddleware:
                        _fastapi_turbo_wrapped_middleware = original_cls

                        def __init__(self, app=None, **bound_kwargs):
                            self._inner = original_cls(app, *args, **bound_kwargs)

                        async def __call__(self, scope, receive, send):
                            result = self._inner(scope, receive, send)
                            if inspect.isawaitable(result):
                                await result

                    _BoundMiddleware.__name__ = getattr(
                        original_cls, "__name__", "BoundMiddleware"
                    )
                    _BoundMiddleware.__qualname__ = getattr(
                        original_cls, "__qualname__", _BoundMiddleware.__name__
                    )
                    _BoundMiddleware.__module__ = getattr(
                        original_cls, "__module__", __name__
                    )
                    middleware_cls = _BoundMiddleware
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
        """Convert the middleware stack into dicts the Rust core can consume.

        Resolves each entry via ``_tower_type_for`` so both the clone marker
        classes AND the real Starlette classes (post-flip) — plus string
        shorthand — map to the same Tower layer config."""
        config: list[dict[str, Any]] = []
        for cls, kwargs in self._middleware_stack:
            mw_type = _tower_type_for(cls)
            if mw_type == "trustedhost":
                config.append({
                    "type": "trustedhost",
                    "allowed_hosts": kwargs.get("allowed_hosts", ["*"]),
                })
            elif mw_type == "httpsredirect":
                config.append({"type": "httpsredirect"})
            elif mw_type:
                # cors / gzip / string shorthand / other known Tower mapping
                config.append({"type": mw_type, **kwargs})
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

    def add_event_handler(self, event_type: str, func: Callable) -> None:
        if event_type == "startup":
            self._on_startup.append(func)
        elif event_type == "shutdown":
            self._on_shutdown.append(func)

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
        from fastapi_turbo.requests import _door_make_request
        scope = _current_request_scope.get() or {}
        request = _door_make_request({**scope, "type": "http", "app": self})
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
        from fastapi_turbo.requests import _door_make_request
        scope = _current_request_scope.get() or {}
        request = _door_make_request({**scope, "type": "http", "app": self})
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

    async def _ainvoke_exception_handler(self, exc: BaseException):
        """Async twin of ``_invoke_exception_handler`` for the async-inline
        path: runs INSIDE a live event-loop task, so a coroutine handler is
        awaited directly (once — matching real FastAPI) instead of the
        send(None)-probe + new-event-loop fallback, which cannot run on a
        thread whose loop is already running."""
        _maybe_sentry_capture_failed_request(exc)
        handler = self._lookup_exception_handler(exc)
        if handler is None:
            return None
        from fastapi_turbo.requests import _door_make_request
        scope = _current_request_scope.get() or {}
        request = _door_make_request({**scope, "type": "http", "app": self})
        try:
            if inspect.iscoroutinefunction(handler):
                return await handler(request, exc)
            return handler(request, exc)
        except Exception:  # noqa: BLE001
            return None

    def _door_handle_dep_exception(self, exc: BaseException):
        """Door dep-resolution error path: route a dependency-raised exception
        through user ``@app.exception_handler`` handlers, mirroring the Python
        dispatcher (which calls ``_invoke_exception_handler`` for dep raises).
        The Rust door previously rendered the default 500 directly via
        ``pyerr_to_response``, silently bypassing user handlers for exceptions
        raised INSIDE a dependency (handler-body raises already go through the
        compiled exception wrapper).

        Returns a Response-like object to send, or None to let Rust render the
        default (``pyerr_to_response`` — which also captures the exception for
        TestClient re-raise). HTTPException keeps Rust's fast path.
        """
        from fastapi_turbo.exceptions import HTTPException as _HE

        if isinstance(exc, _HE):
            return None  # Rust pyerr_to_response renders HTTPException (parity)
        if not self.exception_handlers:
            return None
        result = self._invoke_exception_handler(exc)
        if result is None:
            return None  # no matching handler → Rust default 500 (+ captures)
        # A handler produced a response. Capture for TestClient re-raise UNLESS
        # a SPECIFIC (non-Exception) handler matched — FA semantics: a specific
        # handler means "handled, don't re-raise", while the bare
        # ``Exception`` catch-all still re-raises (Starlette
        # ServerErrorMiddleware parity). When we return None above, Rust's
        # pyerr_to_response does the capture, so capture happens exactly once.
        handled_by_specific = any(
            isinstance(cls, type) and cls is not Exception and isinstance(exc, cls)
            for cls in self.exception_handlers
        )
        if not handled_by_specific:
            captured = getattr(self, "_captured_server_exceptions", None)
            if captured is not None:
                captured.append(exc)
        return result

    # ------------------------------------------------------------------
    # Route collection & introspection
    # ------------------------------------------------------------------

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
        merged.extend(getattr(route, "dependencies", []) or [])
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
        # FA parity: a ``DefaultPlaceholder`` (``Default(generate_unique_id)``,
        # what real APIRouter/strawberry pass when the user did NOT set one)
        # means UNSET — fall through to the next cascade level. If every
        # level is unset, return None so real ``get_openapi`` applies its own
        # default on the CONVERTED route (which carries the full include
        # prefix in ``path``; the live route object here does not, so calling
        # the default fn on it would drop the prefix from the operationId).
        from fastapi_turbo.datastructures import DefaultPlaceholder as _DP

        def _set_or_none(v):
            return None if v is None or isinstance(v, _DP) else v

        fn = (
            _set_or_none(getattr(route, "generate_unique_id_function", None))
            or _set_or_none(getattr(router, "generate_unique_id_function", None))
            or _set_or_none(include_fn)
            or _set_or_none(getattr(self, "generate_unique_id_function", None))
        )
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
            # ``methods`` is normally a list (the turbo APIRoute re-stamps
            # it), but guard with ``next(iter(...))`` in case a raw real
            # APIRoute (set-typed methods) reaches this cascade.
            return fn(route, next(iter(route.methods or ["GET"])).lower())

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

            if _looks_like_starlette_mount(route):
                mount_path = full_path if full_path == "/" else full_path.rstrip("/")
                mounted_app = _mounted_route_asgi_app(type(self), route)
                if isinstance(mounted_app, FastAPI):
                    sub_routes = mounted_app._collect_all_routes()
                    mount_prefix = "" if mount_path == "/" else mount_path.rstrip("/")
                    for r in sub_routes:
                        original = r["path"]
                        r["path"] = mount_prefix + ("" if original == "/" else original)
                        if not r["path"]:
                            r["path"] = "/"
                        r["_from_mount"] = mount_path
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
                            ep = r.get("endpoint")
                            if ep is not None:
                                try:
                                    ctx = getattr(ep, "_fastapi_turbo_endpoint_ctx", None)
                                    if isinstance(ctx, dict):
                                        ctx["path"] = r["path"]
                                except Exception as _exc:  # noqa: BLE001
                                    _log.debug("silent catch in applications: %r", _exc)
                    collected.extend(sub_routes)
                elif isinstance(mounted_app, APIRouter):
                    collected.extend(
                        self._collect_routes_from_router(
                            mounted_app,
                            prefix=mount_path,
                            include_deps=include_deps,
                            include_responses=include_responses,
                            include_deprecated=include_deprecated,
                            include_in_schema=include_in_schema,
                            include_default_response_class=include_default_response_class,
                            include_generate_unique_id_function=include_generate_unique_id_function,
                            include_callbacks=include_callbacks,
                        )
                    )
                elif callable(mounted_app):
                    collected.extend(self._build_asgi_mount_routes(mount_path, mounted_app))
                continue

            is_websocket = (
                getattr(route, "_is_websocket", False)
                or _looks_like_starlette_websocket_route(route)
            )

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
                ws_endpoint = _adapt_websocket_endpoint_class(route.endpoint)
                wrapped_ws = _wrap_websocket_endpoint(
                    self, ws_endpoint, full_path, extra_dependencies=merged_ws_deps,
                )
                collected.append(
                    {
                        "path": full_path,
                        "methods": ["GET"],
                        "endpoint": wrapped_ws,
                        "is_async": inspect.iscoroutinefunction(wrapped_ws),
                        "handler_name": getattr(route, "name", None),
                        "tags": extra_tags + list(getattr(route, "tags", []) or []),
                        "params": [],
                        "is_websocket": True,
                    }
                )
                continue

            if getattr(route, "_fastapi_turbo_starlette_passthrough", False):
                raw_starlette_endpoint = route.endpoint

                async def _starlette_passthrough_endpoint(
                    request, _ep=raw_starlette_endpoint
                ):
                    return await _run_starlette_http_endpoint(_ep, request)

                _starlette_passthrough_endpoint.__name__ = (
                    getattr(route, "name", None)
                    or getattr(raw_starlette_endpoint, "__name__", "route")
                )
                try:
                    _starlette_passthrough_endpoint._fastapi_turbo_route_obj = route  # type: ignore[attr-defined]
                except (AttributeError, TypeError):
                    pass
                collected.append({
                    "path": full_path,
                    "methods": list(getattr(route, "methods", None) or ["GET"]),
                    "endpoint": _starlette_passthrough_endpoint,
                    "is_async": True,
                    "handler_name": getattr(route, "name", None),
                    "tags": extra_tags + list(getattr(route, "tags", []) or []),
                    "params": [_request_injection_param()],
                    "is_websocket": False,
                    "status_code": getattr(route, "status_code", None) or 200,
                    "summary": getattr(route, "summary", None),
                    "description": getattr(route, "description", None),
                    "response_description": getattr(
                        route,
                        "response_description",
                        "Successful Response",
                    ),
                    "responses": {
                        **self.responses,
                        **include_responses,
                        **getattr(router, "responses", {}),
                        **getattr(route, "responses", {}),
                    },
                    "response_model": None,
                    "response_class": _unset_to_none(
                        getattr(route, "response_class", None)
                    ),
                    "deprecated": (
                        bool(getattr(route, "deprecated", False))
                        or bool(getattr(router, "deprecated", False))
                        or bool(include_deprecated)
                    ),
                    "operation_id": getattr(route, "operation_id", None),
                    "include_in_schema": (
                        getattr(route, "include_in_schema", True)
                        and include_in_schema
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
                # Wrap the route-class endpoint in the SAME
                # ``@app.middleware('http')`` chain the regular compile
                # path applies at ``_wrap_with_http_middlewares``. Without
                # this, headers added by ``app.middleware('http')`` (e.g.
                # ``X-App-Mw``) are dropped on routes registered through a
                # custom ``route_class`` — the oneshot door would bypass
                # the app HTTP middleware entirely. The
                # ``_wrap_with_http_middlewares`` helper speaks the
                # Rust-synthesised-kwargs calling convention, so it can't wrap
                # this ``(request) -> response`` adapter; we compose the
                # ``(request, call_next)`` chain inline instead (declaration
                # order, last-decorated outermost).
                http_mws_for_custom = [
                    m
                    for m in (getattr(self, "_http_middlewares", None) or [])
                    if not getattr(m, "_fastapi_turbo_is_asgi_shim", False)
                ]
                for _mw in http_mws_for_custom:
                    _inner_ep = custom_ep

                    async def _wrapped_custom(request, *, _mw=_mw, _inner=_inner_ep):
                        return await _mw(request, _inner)

                    custom_ep = _wrapped_custom
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
                    "methods": sorted(route.methods),
                    "endpoint": custom_ep,
                    "is_async": True,
                    "handler_name": route.name,
                    "tags": extra_tags + route.tags,
                    "params": custom_params,
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
                    "response_class": _unset_to_none(
                        getattr(route, "response_class", None)
                    ),
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

            # ── Pivot end-state: NO eager clone introspection/compile. Every
            # normal HTTP route is served by the ADAPTER (real route.dependant →
            # ParamInfo) or by real-FastAPI DELEGATION in _build_server_args
            # (force-delegated if both first passes decline). The rd carries only
            # the RAW endpoint + route metadata: nothing reads clone param dicts
            # (OpenAPI uses the real route; the door fallback force-delegates).
            merged_deps = self._get_all_dependencies_for_route(
                router, route, include_deps=include_deps
            )
            endpoint = route.endpoint
            params: list = []
            _endpoint_door = None
            is_async = inspect.iscoroutinefunction(endpoint) or _is_async_callable(endpoint)
            response_model = getattr(route, "response_model", None)
            # ``-> None`` (incl. stringified ``-> "None"``) is stored as NoneType by
            # the decoration layer. Pass None to the real-route rebuild so real
            # FastAPI infers it from the endpoint annotation itself — an EXPLICIT
            # response_model=NoneType creates a response field, tripping FA's
            # "no body for 204/304" assertion.
            if response_model is type(None):
                response_model = None
            response_class = _unset_to_none(getattr(route, "response_class", None))
            # Cascade default_response_class: route → router → include-level → app
            if response_class is None:
                response_class = getattr(router, "default_response_class", None)
            if response_class is None and include_default_response_class is not None:
                response_class = include_default_response_class
            if response_class is None:
                response_class = getattr(self, "default_response_class", None)

            # FA 0.120+ ``strict_content_type=False`` — closest-wins precedence:
            # route → router → app. Carried on the rd (NOT stamped on the raw
            # endpoint, which may be shared across apps); build_router copies it
            # onto whichever handler (adapter / delegated) serves the route.
            _route_strict = _unset_to_none(getattr(route, "strict_content_type", None))
            _router_strict = _unset_to_none(
                getattr(router, "strict_content_type", None)
            )
            if _route_strict is not None:
                _strict_effective = _route_strict
            elif _router_strict is not None:
                _strict_effective = _router_strict
            else:
                _strict_effective = self.strict_content_type
            _lax = _strict_effective is False
            # Attach the original route object so Rust can populate
            # ``request.scope["route"]`` — ``test_route_scope`` asserts.
            try:
                endpoint._fastapi_turbo_route_obj = route  # type: ignore[attr-defined]
            except (AttributeError, TypeError):
                pass
            collected.append(
                {
                    "path": full_path,
                    "methods": sorted(route.methods),
                    "endpoint": endpoint,
                    "_endpoint_door": _endpoint_door,
                    "is_async": is_async,
                    "handler_name": route.name,
                    "tags": extra_tags + route.tags,
                    "params": params,
                    # Combined global/include/router/route dependencies — lets the
                    # pivot adapter rebuild a real FastAPI route with the correct
                    # effective dependency graph (Stage D).
                    "_combined_dependencies": merged_deps,
                    "_route_obj": route,
                    "_lax_content_type": _lax,
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
            # default. ``raw_headers.clear()`` resets the MutableHeaders view
            # (real Starlette has no ``headers.clear()``).
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

    def _openapi_real_callbacks(self, clone_callbacks) -> list | None:
        """Rebuild clone callback ``APIRoute``s as real ones (recursively) so real
        ``get_openapi`` documents the operation's ``callbacks`` (it can't process
        clone routes). Callback paths are OpenAPI runtime expressions (e.g.
        ``{$callback_url}/...``) — real ``APIRoute`` accepts them. Returns ``None``
        on any failure so the caller falls back to the clone generator."""
        _RealRoute = _real_fastapi.routing.APIRoute
        _http = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "TRACE")
        out: list = []
        for c in clone_callbacks or []:
            # A callback list item may be a ROUTER (callbacks=[cb_router]) rather
            # than a route — flatten it into its routes.
            if not hasattr(c, "endpoint") and getattr(c, "routes", None) is not None:
                nested_router = self._openapi_real_callbacks(c.routes)
                if nested_router is None:
                    return None
                out.extend(nested_router)
                continue
            cp = getattr(c, "path", None)
            cep = getattr(c, "endpoint", None)
            if cp is None or cep is None:
                return None
            cm = [m for m in (getattr(c, "methods", None) or []) if m in _http] or getattr(
                c, "methods", None
            )
            nested = getattr(c, "callbacks", None)
            real_nested = self._openapi_real_callbacks(nested) if nested else None
            if nested and real_nested is None:
                return None
            try:
                out.append(
                    _RealRoute(
                        cp,
                        cep,
                        methods=cm,
                        response_model=getattr(c, "response_model", None),
                        status_code=getattr(c, "status_code", None),
                        tags=getattr(c, "tags", None) or None,
                        summary=getattr(c, "summary", None),
                        description=getattr(c, "description", None) or "",
                        response_description=getattr(
                            c, "response_description", "Successful Response"
                        ),
                        responses=getattr(c, "responses", None) or None,
                        deprecated=getattr(c, "deprecated", None),
                        operation_id=getattr(c, "operation_id", None),
                        include_in_schema=getattr(c, "include_in_schema", True),
                        name=getattr(c, "name", None),
                        openapi_extra=getattr(c, "openapi_extra", None),
                        callbacks=real_nested,
                    )
                )
            except Exception:
                return None
        return out

    def _openapi_real_routes(self, route_dicts: list[dict], webhook: bool = False) -> list | None:
        """Build a real ``fastapi.routing.APIRoute`` per clone route dict so real
        ``fastapi.openapi.utils.get_openapi`` can generate the schema (the OpenAPI
        pivot). Returns the route list, or ``None`` if ANY route can't be built
        real (caller falls back to the clone generator) — so the real path is
        never worse than the clone path. WebSocket routes are skipped (get_openapi
        documents only HTTP ``APIRoute``s)."""
        _RealRoute = _real_fastapi.routing.APIRoute
        _http = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "TRACE")
        real_routes: list = []
        for rd in route_dicts:
            if rd.get("is_websocket"):
                continue
            route = rd.get("_route_obj")
            endpoint = getattr(route, "endpoint", None) if route is not None else None
            if route is None or endpoint is None:
                # Internal/dynamic routes (/openapi.json, /docs, /redoc) carry no
                # clone route object; they're include_in_schema=False so real
                # get_openapi omits them anyway — skip, don't fall back.
                continue
            methods = [m for m in (rd.get("methods") or []) if m in _http] or rd.get(
                "methods"
            )
            # response_class only when it's a real Starlette Response subclass
            # (responses.py re-exports real, so clone routes carry real classes);
            # else let real APIRoute use its default so the media type is canonical.
            # rd["response_class"] is the RESOLVED class (route → router → app
            # default_response_class); the route object alone may carry None.
            rc = rd.get("response_class") or _unset_to_none(
                getattr(route, "response_class", None)
            )
            try:
                rc_ok = isinstance(rc, type) and issubclass(
                    rc, _real_starlette_response
                )
            except TypeError:
                rc_ok = False
            # SSE / NDJSON streaming endpoints: the clone stores the return
            # annotation (e.g. AsyncIterable[Item]) as response_model, which real
            # get_dependant can't field. Build with response_model=None, then set
            # real's native streaming attrs (is_sse_stream / is_json_stream +
            # stream_item_field) AFTER the build so real get_openapi emits the
            # spec SSE event envelope / jsonl itemSchema + lands the model.
            _rm = getattr(route, "response_model", None)
            _needs_none, _is_sse, _is_json, _inner = _oa_stream_info(_rm, rc, endpoint)
            if _needs_none:
                _rm = None
            # Rebuild clone callback routes → real so real get_openapi documents
            # the operation's ``callbacks`` (real can't process clone routes).
            # rd["callbacks"] is the COMBINED set (route + include + app level); the
            # route object alone only carries the route-level ones.
            _cbs = rd.get("callbacks") or getattr(route, "callbacks", None)
            _real_cbs = self._openapi_real_callbacks(_cbs) if _cbs else None
            if _cbs and _real_cbs is None:
                return None
            kw: dict = dict(
                methods=methods,
                dependencies=(
                    rd.get("_combined_dependencies")
                    or getattr(route, "dependencies", None)
                    or None
                ),
                response_model=_rm,
                status_code=getattr(route, "status_code", None),
                # rd["tags"]/["responses"] are the COMBINED include + route level
                # (the route object alone carries only its own level).
                tags=rd.get("tags") or getattr(route, "tags", None) or None,
                summary=getattr(route, "summary", None),
                description=getattr(route, "description", None) or "",
                response_description=getattr(
                    route, "response_description", "Successful Response"
                ),
                # App-level ``responses`` merge under the combined route/include
                # responses (clone behavior; real reads only route-level). The
                # more-specific (route/include) wins.
                responses=(
                    {**(getattr(self, "responses", None) or {}),
                     **(rd.get("responses") or getattr(route, "responses", None) or {})}
                    or None
                ),
                # rd["deprecated"] carries include_router(deprecated=...) inheritance;
                # the route object alone has only its own level.
                deprecated=rd.get("deprecated") or getattr(route, "deprecated", None),
                # The clone already resolves the operationId via the full cascade
                # (route → router → include → app generate_unique_id_function) and
                # stamps it on rd; pass it through (real uses it verbatim). None →
                # real's default generate_unique_id, which matches the clone default.
                operation_id=rd.get("operation_id") or getattr(route, "operation_id", None),
                response_model_include=getattr(route, "response_model_include", None),
                response_model_exclude=getattr(route, "response_model_exclude", None),
                response_model_by_alias=getattr(route, "response_model_by_alias", True),
                response_model_exclude_unset=getattr(
                    route, "response_model_exclude_unset", False
                ),
                response_model_exclude_defaults=getattr(
                    route, "response_model_exclude_defaults", False
                ),
                response_model_exclude_none=getattr(
                    route, "response_model_exclude_none", False
                ),
                include_in_schema=getattr(route, "include_in_schema", True),
                name=getattr(route, "name", None),
                callbacks=_real_cbs,
            )
            # Clone OpenAPI extensions the real fork's get_openapi has no route
            # kwarg for → fold into openapi_extra (real deep_dict_updates it into
            # the operation). servers/external_docs are operation-level; an
            # explicit route ``security`` (incl. ``[]`` to disable) overrides the
            # dep-derived security.
            oe = dict(getattr(route, "openapi_extra", None) or {})
            if getattr(route, "servers", None):
                oe["servers"] = route.servers
            if getattr(route, "external_docs", None):
                oe["externalDocs"] = route.external_docs
            if getattr(route, "security", None) is not None:
                oe["security"] = route.security
            kw["openapi_extra"] = oe or None
            if rc_ok:
                kw["response_class"] = rc
            # Webhooks are keyed by NAME (no leading-slash normalization) — use the
            # route's own path; regular routes use rd["path"] (mount-prefixed).
            path = getattr(route, "path", None) if webhook else rd["path"]
            try:
                _real = _RealRoute(path or rd["path"], endpoint, **kw)
            except Exception:
                return None
            if _is_sse or _is_json:
                _oa_apply_stream(_real, _is_sse, _is_json, _inner)
            real_routes.append(_real)
        return real_routes

    def _openapi_real(self, route_dicts: list[dict], webhook_dicts: list[dict],
                      effective_servers) -> dict:
        """Generate the OpenAPI schema via REAL ``fastapi.openapi.utils.get_openapi``
        over real ``APIRoute``s rebuilt from the clone routes. This is the SOLE
        generator (clone ``_openapi.py`` is deleted); errors propagate like real
        FastAPI's. NOT ``from fastapi.openapi.utils import`` (the shim rebinds that);
        ``_real_fastapi`` is the real module captured pre-shim."""
        real_routes = self._openapi_real_routes(route_dicts)
        real_webhooks = (
            self._openapi_real_routes(webhook_dicts, webhook=True) if webhook_dicts else []
        )
        if real_routes is None or real_webhooks is None:
            raise RuntimeError(
                "fastapi-turbo: could not rebuild real OpenAPI routes for this app"
            )
        return _real_fastapi.openapi.utils.get_openapi(
            title=self.title,
            version=self.version,
            openapi_version=self.openapi_version,
            summary=self.summary,
            description=self.description,
            routes=real_routes,
            webhooks=real_webhooks,
            tags=self.openapi_tags,
            servers=effective_servers,
            terms_of_service=self.terms_of_service,
            contact=self.contact,
            license_info=self.license_info,
            separate_input_output_schemas=self.separate_input_output_schemas,
            external_docs=self.external_docs,
        )

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
        # OpenAPI is generated by REAL fastapi.openapi.utils.get_openapi over real
        # APIRoutes rebuilt from the clone routes (the clone _openapi.py generator
        # is deleted). Errors propagate like real FastAPI's.
        self.openapi_schema = self._openapi_real(
            route_dicts, webhook_dicts, effective_servers
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
    # running on that loop (e.g. the lifespan-MW dispatcher below) and —
    # more importantly — runs handlers on the worker thread's loop,
    # decoupling lifespan-created asyncio resources (asyncpg pools,
    # ``redis.asyncio`` clients, aiohttp sessions) from the request
    # loop awaiting ``__call__``. These coroutine variants do the work
    # inline on the caller's loop. They carry the same ``_startup_state``
    # machine as the sync variants so a failed startup poisons the app
    # for subsequent requests instead of silently re-firing.
    async def _async_run_startup_handlers(self) -> None:
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
        self._startup_state = "running"
        try:
            for handler in self._collect_startup_handlers():
                if inspect.iscoroutinefunction(handler):
                    await handler()
                else:
                    handler()
        except Exception as exc:
            self._startup_state = "failed"
            self._startup_failure = exc
            raise
        else:
            self._startup_state = "started"

    async def _async_run_shutdown_handlers(self) -> None:
        for handler in self._collect_shutdown_handlers():
            if inspect.iscoroutinefunction(handler):
                await handler()
            else:
                handler()
        self._startup_state = "not_started"
        self._startup_failure = None
        self._in_process_dynamic_routes_installed = False

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
                    getattr(r, "_fastapi_turbo_dynamic_route", False)
                    and ep is not None
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
            _route._fastapi_turbo_dynamic_route = True
            _route._fastapi_turbo_bypass_deps = True
            self.router.routes.insert(0, _route)

        # Swagger UI / ReDoc HTML routes — Rust path bakes these
        # into ``run_server``; for the in-process path we register
        # Python handlers that return the HTML produced by the
        # ``fastapi.openapi.docs`` helpers.
        if self.docs_url is not None and _openapi_url_val:
            try:
                import importlib as _importlib
                _docs_mod = _importlib.import_module("fastapi.openapi.docs")
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
                _swag_route._fastapi_turbo_dynamic_route = True
                _swag_route._fastapi_turbo_bypass_deps = True
                self.router.routes.insert(0, _swag_route)

        if self.redoc_url is not None and _openapi_url_val:
            try:
                import importlib as _importlib
                _docs_mod = _importlib.import_module("fastapi.openapi.docs")
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
                _redoc_route._fastapi_turbo_dynamic_route = True
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
                import importlib as _importlib
                _docs_mod = _importlib.import_module("fastapi.openapi.docs")
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
                _oauth2_route._fastapi_turbo_dynamic_route = True
                _oauth2_route._fastapi_turbo_bypass_deps = True
                self.router.routes.insert(0, _oauth2_route)

        self._in_process_dynamic_routes_installed = True

    def run(
        self,
        host: str = "127.0.0.1",
        port: int = 8000,
        workers: int | None = None,
        **kwargs: Any,
    ) -> None:
        """Collect routes, hand them to the Rust core, and start serving.

        ``workers`` defaults to one process per CPU (``os.cpu_count()``); pass
        ``workers=1`` for a single process. Multi-worker forks an fd-passing
        acceptor (this process) + N worker processes — each runs the full
        router on fd-passed connections, covering every transport (HTTP/1,
        HTTP/2, WebSocket, SSE). Auto-falls-back to a single process when
        forking isn't safe (called off the main thread, e.g. ``TestClient``;
        or a platform without ``os.fork``). ``FASTAPI_TURBO_WORKERS`` overrides.
        """
        from fastapi_turbo._fastapi_turbo_core import run_server

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

        # Multi-worker (default = one per CPU): fork the fd-passing acceptor
        # (this process) + N workers. Workers run their own startup/lifespan
        # post-fork so loop-bound resources bind per-worker. Falls back to the
        # single-process path below when forking isn't safe.
        _effective_workers = self._resolve_worker_count(workers)
        if _effective_workers > 1:
            self._run_multiworker(host, port, _effective_workers)
            return

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

        run_server(*self._build_server_args(host, port))

    def _resolve_worker_count(self, workers: int | None) -> int:
        """Resolve the effective worker count. Default = one process per CPU;
        ``FASTAPI_TURBO_WORKERS`` overrides. Falls back to 1 when forking isn't
        safe: no ``os.fork`` (e.g. Windows), or not on the main thread (e.g.
        ``TestClient`` runs ``app.run()`` in a background thread — forking from
        a non-main thread is unsafe)."""
        import threading

        env = os.environ.get("FASTAPI_TURBO_WORKERS")
        if env is not None:
            try:
                workers = int(env)
            except ValueError:
                pass
        if workers is None:
            workers = os.cpu_count() or 1
        try:
            workers = max(1, int(workers))
        except (TypeError, ValueError):
            workers = 1
        if workers > 1:
            if not hasattr(os, "fork"):
                workers = 1
            elif threading.current_thread() is not threading.main_thread():
                workers = 1
        return workers

    def _run_multiworker(self, host: str, port: int, n: int) -> None:
        """Fork the fd-passing acceptor (this process) + ``n`` worker processes.
        Each worker runs its own startup/lifespan, then serves fd-passed
        connections via the Rust ``run_worker`` (all transports). The acceptor
        binds the port and routes each connection to the least-loaded worker.
        Ctrl-C drains the acceptor, then terminates + reaps the workers."""
        import signal
        import sys as _sys
        import tempfile

        from fastapi_turbo._fastapi_turbo_core import run_acceptor, run_worker

        sock_path = os.path.join(
            tempfile.gettempdir(), f"fastapi-turbo-{os.getpid()}-{port}.sock"
        )
        children: list[int] = []
        for i in range(n):
            pid = os.fork()
            if pid == 0:
                # ── child: one worker process ──
                exit_code = 0
                try:
                    # Per-worker startup so loop-bound resources (asyncpg/redis
                    # pools, asyncio primitives) are created in THIS process on
                    # the handler loop — same ordering as single-process run().
                    if self._collect_lifespans():
                        self._run_lifespan_startup()
                    self._run_startup_handlers()
                    run_worker(sock_path, *self._build_server_args(host, port))
                except BaseException as exc:  # noqa: BLE001
                    _sys.stderr.write(f"fastapi-turbo worker {i} exited: {exc!r}\n")
                    exit_code = 1
                finally:
                    os._exit(exit_code)
            children.append(pid)

        # ── parent: the acceptor ──
        def _terminate_children(*_a: object) -> None:
            for cpid in children:
                try:
                    os.kill(cpid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

        try:
            run_acceptor(host, port, sock_path)
        finally:
            _terminate_children()
            for cpid in children:
                try:
                    os.waitpid(cpid, 0)
                except (ChildProcessError, OSError):
                    pass
            try:
                os.unlink(sock_path)
            except OSError:
                pass

    def _route_obj_reusable(self, rd: dict, route, allow_prefixed: bool = False) -> bool:
        """P10.4 collection inversion: True when the decoration-built REAL
        route can BE the adapter/delegation source directly — i.e. the
        collection walker added nothing on top of what the route object's
        own ctor already baked into its ``dependant``/response fields.

        The per-route rebuild in ``_adapter_route_info`` /
        ``_delegated_route_info`` exists precisely for the divergent
        cases, which must KEEP rebuilding:

        - include_router()/router/mount prefixes: ``rd["path"]`` carries
          the full prefixed path; ``route.path`` does not. With
          ``allow_prefixed=True`` (the ADAPTER site) a brace-free added
          prefix still reuses — the dependant only ever consumed the
          path-param NAME set from the path, and the caller passes
          ``ctx_path`` to ``build_handler`` so error contexts keep the
          full path. A ``{param}`` inside the added prefix was classified
          as a QUERY param by the decoration-time dependant, so those
          rebuild. The DELEGATED site requires strict path equality:
          real 0.136's request/response validation-error endpoint
          context renders ``METHOD {route.path}`` from the route object
          itself (upstream's ``test_validation_error_context`` asserts
          the full ``/sub/items/`` mount path);
        - app/include/router-level dependencies: they live ONLY in
          ``rd["_combined_dependencies"]`` and reach a dependant solely
          via the rebuild's ``dependencies=`` ctor arg. The live route's
          dependant holds route-level deps only — and the same route
          object can be included into multiple apps with different
          cascades, so it must NOT be mutated in place;
        - cascaded ``default_response_class`` (router/include/app-level)
          — the route's own attr is a ``DefaultPlaceholder`` when unset;
        - rd's ``-> None``/NoneType ``response_model`` normalization
          (a rebuild-input fix; caught by the ``is`` check below).
        """
        try:
            rd_path = rd["path"]
            route_path = route.path
            if rd_path != route_path:
                if not allow_prefixed:
                    return False
                # Prefixed (include_router/router-prefix/mount): reusable
                # only when the ADDED prefix is brace-free — it then adds
                # no path-param names, which is all the decoration-time
                # dependant consumed from the path.
                if not rd_path.endswith(route_path):
                    return False
                if "{" in rd_path[: len(rd_path) - len(route_path)]:
                    return False
            merged = rd.get("_combined_dependencies") or []
            own = list(getattr(route, "dependencies", None) or [])
            if len(merged) != len(own) or any(
                a is not b for a, b in zip(merged, own)
            ):
                return False
            if rd.get("response_class") is not _unset_to_none(
                getattr(route, "response_class", None)
            ):
                return False
            if rd.get("response_model") is not getattr(route, "response_model", None):
                return False
        except (KeyError, TypeError, AttributeError):
            return False
        return True

    def _adapter_route_info(self, rd: dict, for_door_mix: bool = False):
        """Stage D: drive a route's door params off REAL FastAPI introspection
        instead of the clone's ``_introspect``.

        Rebuilds a real ``fastapi.routing.APIRoute`` from the route's effective
        config (full path, original endpoint, combined dependencies, response_model
        + flags) and maps it through the pivot adapter. Returns
        ``(params, handler, is_async)`` for the Rust door, or ``None`` to fall back
        to the clone path — for WebSocket/mounted routes, non-default response
        classes (the adapter applies response_model but not a custom response_class),
        or anything the adapter declines (e.g. async-generator deps)."""
        if rd.get("is_websocket") or rd.get("_from_mount"):
            return None
        # dependency_overrides (a testing feature) is resolved at request time by
        # the clone path; the adapter bakes the real callable into ParamInfo and
        # can't honor a runtime override. Delegate everything while any override is
        # registered. Also covers a non-default response class (custom rendering).
        if getattr(self, "dependency_overrides", None):
            return None
        route = rd.get("_route_obj")
        if route is None:
            return None
        # The ORIGINAL user endpoint — rd["endpoint"] is the clone-compiled
        # ``(**kwargs)`` wrapper for dep routes, which real FastAPI would
        # mis-introspect as a ``kwargs`` query param.
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            return None
        # Bare generator endpoints (sync/async) auto-wrap into a streaming
        # response (NDJSON, or the custom response_class for SSE). The adapter
        # introspects the generator's params off real FastAPI and builds the wrap
        # via _build_stream_handler (below) — so generators ride the fast adapter
        # path (the door streams the result, 7.3a) instead of the clone.
        import inspect as _insp_gen

        _is_gen = _insp_gen.isgeneratorfunction(endpoint) or _insp_gen.isasyncgenfunction(
            endpoint
        )
        # Param markers (Depends/Query/Header/...) are now bridged to real FastAPI's
        # (see param_functions / dependencies), so real introspection recognizes
        # them. The generic name->kind net below still catches any clone TYPE real
        # FastAPI can't see yet (UploadFile/Request/Response/BackgroundTasks), so a
        # mis-introspected route delegates rather than mis-serves.
        # Route/router/global dependencies are now ENGAGED: rd["_combined_dependencies"]
        # is the full effective set (app + include + router + route via
        # _get_all_dependencies_for_route) and is passed to the real APIRoute below;
        # the adapter emits route-level deps (name=None) as non-handler-params so they
        # run for side effects (auth) without being passed to the handler.
        # A custom status_code IS now carried on RouteInfo (the door applies it as
        # the default status for non-Response results, overridable by a handler/dep
        # ``response.status_code``), so custom-status routes ride the adapter — EXCEPT
        # when the app has @app.middleware("http"): the chain renders the handler's
        # result into a Response, past the door's default-status hook, so keep those
        # on the clone path (which bakes the status into that Response).
        if rd.get("status_code") not in (None, 200) and getattr(
            self, "_http_middlewares", None
        ):
            return None
        # Custom response_class is now applied by build_handler (it renders the
        # endpoint result via the class) + the door merges any dep-injected
        # Response, so these routes ride the fast adapter path (~32K) not the clone.
        # Clone framework TYPES (Request/Response/BackgroundTasks/UploadFile/
        # WebSocket) are reimplementations, NOT real starlette subclasses, so real
        # FastAPI's introspection can't see them. Check the SIGNATURE and decline
        # BEFORE building the real route — building it runs real get_dependant which
        # can mutate the shared endpoint/markers, corrupting the clone path even for
        # a route that ultimately declines.
        if _signature_uses_clone_framework_type(endpoint):
            return None
        try:
            import inspect as _inspect

            # Must be REAL FastAPI's APIRoute (the shim rebinds ``fastapi.routing``
            # to the clone) — it builds the real ``dependant`` the adapter reads.
            _RealRoute = _real_fastapi.routing.APIRoute

            from fastapi_turbo._introspect_from_real_fastapi import (
                Undelegable,
                build_handler,
                extract_params_from_route,
            )
        except Exception:
            return None
        try:
            if self._route_obj_reusable(rd, route, allow_prefixed=True):
                # P10.4 inversion: the decoration-built real-APIRoute subclass
                # already carries the exact dependant/response fields the
                # rebuild below would reproduce — use it as the source of
                # truth (skips a per-route real-APIRoute construction on
                # every _build_server_args / door re-registration).
                real = route
            else:
                _http_methods = [
                    m
                    for m in rd["methods"]
                    if m
                    in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "TRACE")
                ]
                real = _RealRoute(
                    rd["path"],
                    endpoint,
                    methods=_http_methods or rd["methods"],
                    dependencies=rd.get("_combined_dependencies") or None,
                    # A generator return is AsyncIterable[Item] / Iterable[Item] which
                    # real get_dependant can't field — build with response_model=None;
                    # item validation happens in _build_stream_handler instead.
                    response_model=None if _is_gen else rd.get("response_model"),
                    status_code=(
                        rd["status_code"] if rd.get("status_code") not in (None, 200) else None
                    ),
                    response_class=rd.get("response_class") or _real_fastapi.datastructures.Default(
                        _real_fastapi.responses.JSONResponse
                    ),
                    response_model_include=getattr(route, "response_model_include", None),
                    response_model_exclude=getattr(route, "response_model_exclude", None),
                    response_model_by_alias=getattr(route, "response_model_by_alias", True),
                    response_model_exclude_unset=getattr(
                        route, "response_model_exclude_unset", False
                    ),
                    response_model_exclude_defaults=getattr(
                        route, "response_model_exclude_defaults", False
                    ),
                    response_model_exclude_none=getattr(
                        route, "response_model_exclude_none", False
                    ),
                )
            # SecurityScopes: the adapter accumulates the ``Security(...,
            # scopes=[...])`` chain into each dependant's ``oauth_scopes`` and the
            # door builds ``SecurityScopes(scopes=...)`` from it (with scope-aware
            # per-request dep caching), so these routes run on the fast adapter path.
            params = extract_params_from_route(real, app=self)
            # Generators: build the streaming wrap (NDJSON or the custom
            # response_class for SSE) instead of build_handler's await-the-endpoint
            # path (which can't await an async-generator function).
            if _is_gen:
                handler = _build_stream_handler(
                    endpoint, rd.get("response_model"), rd.get("response_class"), self
                )
            else:
                handler = build_handler(real, ctx_path=rd["path"])
        except Undelegable:
            return None
        except Exception:
            return None
        # A leaked ``**kwargs``/``*args`` dep-input means real FastAPI couldn't
        # introspect a callable — e.g. a clone security scheme whose ``__call__`` is
        # ``(self, *args, **kwargs)`` and which isn't a real ``SecurityBase``.
        # Delegate those routes to the clone path.
        if any(p.name.endswith("__kwargs") or p.name.endswith("__args") for p in params):
            return None
        # The adapter's handler-facing params must exactly cover the endpoint's own
        # signature (a dep contributes its result name; its extracted inputs are
        # is_handler_param=False). A divergence means something was dropped or
        # mis-mapped — delegate.
        try:
            sig_names = {
                name
                for name, p in _inspect.signature(endpoint).parameters.items()
                if p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
            }
        except (TypeError, ValueError):
            return None
        adapter_names = {p.name for p in params if p.is_handler_param}
        if adapter_names != sig_names:
            return None
        # Match the clone's compile order so the door drives the adapter handler
        # identically: async → SYNC submit-caller (running loop from the first
        # instruction), then wrap in the ``@app.middleware("http")`` chain so those
        # middlewares (which the clone applies by wrapping the handler) still run.
        if _inspect.iscoroutinefunction(handler):
            from fastapi_turbo._door_support import (
                _has_await_in_source,
                _make_sync_wrapper,
                _uses_running_loop,
            )

            if (
                _async_inline_enabled()
                and not getattr(self, "_http_middlewares", None)
                and not any(p.kind == "dependency" for p in params)
                and (_has_await_in_source(handler) or _uses_running_loop(handler))
            ):
                # FASTAPI_TURBO_ASYNC_INLINE: register the coroutine function
                # itself (is_async=True) so the door drives the request on the
                # worker loop end-to-end — no SYNC submit-caller, no Event
                # handoff. Pre-mark needs-worker so the door NEVER probes it
                # with send(None) (a probe-close on these known-suspending
                # handlers could double-run pre-await side effects). No-await
                # handlers keep the classic wrap (their probe path is faster).
                inline_handler = _wrap_with_exception_handlers_async(handler, self)
                try:
                    inline_handler._fastapi_turbo_needs_worker = True
                    if inline_handler is not handler:
                        inline_handler._fastapi_turbo_original_endpoint = handler
                except (AttributeError, TypeError):
                    pass
                else:
                    return params, inline_handler, True
            handler = _make_sync_wrapper(handler, for_handler=True, app=self)
        # Dispatch handler-raised exceptions to the app's custom exception handlers
        # (the clone does this in its compiled handler) — innermost, so middleware
        # below still wraps the resulting response.
        handler = _wrap_with_exception_handlers(handler, self)
        http_mws = getattr(self, "_http_middlewares", None)
        if http_mws:
            from fastapi_turbo._middleware_wrap import _wrap_with_http_middlewares

            # Door mixed Tower+raw-MW path: strip the raw-ASGI shims here too —
            # the door composes them as an OUTER ASGI chain, so baking them in
            # would double-apply. @app.middleware / BaseHTTPMiddleware stay.
            if for_door_mix:
                http_mws = [
                    m
                    for m in http_mws
                    if not getattr(m, "_fastapi_turbo_is_asgi_shim", False)
                ]
            if http_mws:
                handler = _wrap_with_http_middlewares(handler, http_mws, self)
                try:
                    handler._has_http_middleware = True  # door → inject metadata kwargs
                except (AttributeError, TypeError):
                    pass
        return params, handler, False

    def _delegated_route_info(
        self, rd: dict, for_door_mix: bool = False, force: bool = False
    ):
        """Fallback for routes the lean adapter declines (custom response_class,
        custom status_code, UploadFile/Response edges, SecurityScopes, ...): serve
        them via REAL FastAPI's own route handler so the clone-compiled
        ``_introspect`` / ``_resolution`` path isn't needed.

        ``force=True`` is the END-STATE safety net (the clone-compiled fallback is
        gone): skip the opt-out flag and the narrow proving declines (sync-gen
        endpoints, async-gen deps, mounted sub-FastAPI routes) and RAISE on a
        construction failure instead of returning None — a loud startup error
        beats silently mis-serving a route.

        ``route.get_route_handler()`` returns ``async (request) -> Response`` that
        runs real FastAPI's full pipeline (validation, dependency resolution,
        ``response_model`` serialization, status/headers). We register it as a
        single ``inject_request`` param handler — the door builds the Request
        (``needs_body`` includes ``has_inject_request``, so the body is read and
        available for real FastAPI's ``request.form()``/``.json()``), the handler
        returns a real Response the door renders.

        Gated behind ``FASTAPI_TURBO_DELEGATE=1`` (default off) while it's proven
        end-to-end — EXCEPT override-active apps, which ALWAYS delegate: real
        FastAPI's ``solve_dependencies`` + ``dependency_overrides_provider`` (set
        below) resolves ``app.dependency_overrides`` at REQUEST time, including
        different-signature overrides, so overrides no longer need the
        clone-compiled path. WebSocket / mounted routes decline (different
        protocols). Returns ``(params, handler, is_async)`` or ``None``."""
        import os

        # DEFAULT-ON (opt out via FASTAPI_TURBO_DELEGATE=0): adapter declines drain
        # to real FastAPI's own route handler, NOT the clone-compiled path. Override-
        # active apps delegate even under the opt-out (correctness: the clone's
        # override machinery mis-resolves complex different-signature overrides).
        if not force and not getattr(self, "dependency_overrides", None):
            if os.environ.get("FASTAPI_TURBO_DELEGATE") == "0":
                return None
        if rd.get("is_websocket"):
            return None
        if rd.get("_from_mount") and not force:
            return None
        route = rd.get("_route_obj")
        if route is None:
            return None
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            return None
        # Bare ASYNC-generator endpoints: real FastAPI 0.136 natively auto-wraps them
        # into an ``application/jsonl`` StreamingResponse (same as the clone), so
        # delegation handles them — the door streams the returned StreamingResponse.
        # SYNC generators are declined to the clone: real FA wraps them via
        # iterate_in_threadpool, and the door doesn't propagate a mid-stream
        # ResponseValidationError raised from that threadpool iterator (async gens
        # propagate fine). Sync streaming is uncommon and was on the clone already.
        if inspect.isgeneratorfunction(endpoint) and not force:
            return None
        try:
            from fastapi_turbo._fastapi_turbo_core import ParamInfo

            if self._route_obj_reusable(rd, route) and getattr(
                route, "dependency_overrides_provider", None
            ) in (None, self):
                # P10.4 inversion: reuse the decoration-built real-APIRoute
                # subclass directly (its dependant/response fields match what
                # the rebuild below would reproduce). The provider guard keeps
                # a router shared across DIFFERENT apps on the rebuild path —
                # we stamp ``dependency_overrides_provider = self`` below, and
                # that must never be flipped between apps on a live route.
                real = route
            else:
                _RealRoute = _real_fastapi.routing.APIRoute
                _http_methods = [
                    m
                    for m in rd["methods"]
                    if m
                    in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "TRACE")
                ]
                real = _RealRoute(
                    rd["path"],
                    endpoint,
                    methods=_http_methods or rd["methods"],
                    dependencies=rd.get("_combined_dependencies") or None,
                    response_model=rd.get("response_model"),
                    status_code=(
                        rd["status_code"] if rd.get("status_code") not in (None, 200) else None
                    ),
                    response_class=rd.get("response_class") or _real_fastapi.datastructures.Default(
                        _real_fastapi.responses.JSONResponse
                    ),
                    response_model_include=getattr(route, "response_model_include", None),
                    response_model_exclude=getattr(route, "response_model_exclude", None),
                    response_model_by_alias=getattr(route, "response_model_by_alias", True),
                    response_model_exclude_unset=getattr(
                        route, "response_model_exclude_unset", False
                    ),
                    response_model_exclude_defaults=getattr(
                        route, "response_model_exclude_defaults", False
                    ),
                    response_model_exclude_none=getattr(
                        route, "response_model_exclude_none", False
                    ),
                )
            # Async-generator yield-dependencies: their teardown ordering relative
            # to an outer @app.middleware can't be replicated at the handler level
            # (the door applies middleware outside the delegated handler, and a
            # deferred teardown breaks exception propagation + leaks temp files — see
            # the handler comment below). The clone path orders these correctly, so
            # decline them to it (the adapter already declines async-gen deps too).
            def _has_async_gen_dep(dep) -> bool:
                call = getattr(dep, "call", None)
                if call is not None and inspect.isasyncgenfunction(call):
                    return True
                return any(
                    _has_async_gen_dep(s) for s in getattr(dep, "dependencies", []) or []
                )

            if _has_async_gen_dep(real.dependant) and not force:
                return None
            # The standalone route has no provider, so real FastAPI's
            # solve_dependencies can't see ``app.dependency_overrides``. Point it at
            # this app (a real FastAPI subclass) so overrides — added at startup OR
            # at request time (TestClient) — are honored, matching the clone path.
            real.dependency_overrides_provider = self
            real_handler = real.get_route_handler()  # async (request) -> Response
        except Exception:
            if force:
                # End-state: no clone fallback exists — surface the construction
                # failure loudly at startup instead of mis-serving the route.
                raise
            return None

        from contextlib import AsyncExitStack as _AsyncExitStack
        import fastapi.exception_handlers as _fa_eh
        from fastapi.exceptions import RequestValidationError as _RVE

        async def handler(**kwargs):
            request = kwargs["request"]
            # Real FastAPI's route handler reads three nested AsyncExitStacks from the
            # scope (``fastapi_middleware_astack`` files ⊃ ``fastapi_inner_astack``
            # request-scoped deps ⊃ ``fastapi_function_astack`` function-scoped deps),
            # normally set by AsyncExitStackMiddleware + the request_response wrapper —
            # both bypassed when we call get_route_handler() directly. ``async with
            # A, B, C`` tears them down C→B→A INSIDE the request (so file/temp cleanup
            # runs and a yield-dep's after-yield raise propagates to the caller) AND
            # propagates a handler exception into the yield-dep finalizers (so their
            # except/finally observe it) — matching FA's semantics. (Teardown can't be
            # deferred past send to mirror FA's middleware/bg dep-observation ordering:
            # the door owns send, and deferring breaks teardown-exception propagation +
            # leaks temp files. We instead run background tasks before teardown below,
            # which covers the common case.)
            async with (
                _AsyncExitStack() as _file_stack,
                _AsyncExitStack() as _inner_stack,
                _AsyncExitStack() as _function_stack,
            ):
                request.scope["fastapi_middleware_astack"] = _file_stack
                request.scope["fastapi_inner_astack"] = _inner_stack
                request.scope["fastapi_function_astack"] = _function_stack
                try:
                    response = await real_handler(request)
                except _RVE as exc:
                    # The door validates in Rust → 422; a real-FastAPI-raised
                    # RequestValidationError would otherwise be captured as a SERVER
                    # exception (re-raised). Render 422 via a user-registered handler
                    # (specific RVE only — matches FA, where RVE's registered handler
                    # beats the Exception catch-all) or FA's default. HTTPException
                    # propagates (Rust renders it).
                    _uh = (self.exception_handlers or {}).get(_RVE)
                    _res = (_uh or _fa_eh.request_validation_exception_handler)(
                        request, exc
                    )
                    if inspect.isawaitable(_res):
                        _res = await _res
                    return _res
                # Run background tasks before yield-dep teardown (FA order: they
                # observe deps in their pre-teardown state), then clear so the door
                # doesn't double-run them.
                _bg = getattr(response, "background", None)
                if _bg is not None:
                    _bgr = _bg()
                    if inspect.isawaitable(_bgr):
                        await _bgr
                    response.background = None
                return response

        handler.__name__ = getattr(endpoint, "__name__", "endpoint")

        # Mirror _adapter_route_info's wrapping order: async → SYNC submit-caller
        # (the door drives sync handlers), then exception handlers (innermost, so
        # real FastAPI's RequestValidationError/HTTPException dispatch to the app's
        # handlers), then the @app.middleware("http") chain.
        from fastapi_turbo._door_support import _make_sync_wrapper

        if _async_inline_enabled() and not getattr(self, "_http_middlewares", None):
            # FASTAPI_TURBO_ASYNC_INLINE: the delegated pipeline is always a
            # suspending coroutine function (real FastAPI's route handler) with
            # a single inject_request param and no Rust-level deps — register
            # it as genuinely async + pre-marked needs-worker so the door
            # drives it on the worker loop end-to-end (no Event handoff, no
            # send(None) probe).
            inline_handler = _wrap_with_exception_handlers_async(handler, self)
            try:
                inline_handler._fastapi_turbo_needs_worker = True
                if inline_handler is not handler:
                    inline_handler._fastapi_turbo_original_endpoint = handler
                inline_handler._fastapi_turbo_route_obj = route
            except (AttributeError, TypeError):
                pass
            else:
                inline_params = [
                    ParamInfo(
                        name="request",
                        kind="inject_request",
                        type_hint="any",
                        required=False,
                        default_value=None,
                        has_default=False,
                        model_class=None,
                        alias=None,
                        is_handler_param=True,
                        scalar_validator=None,
                    )
                ]
                return inline_params, inline_handler, True

        handler = _make_sync_wrapper(handler, for_handler=True, app=self)
        handler = _wrap_with_exception_handlers(handler, self)
        http_mws = getattr(self, "_http_middlewares", None)
        if http_mws:
            from fastapi_turbo._middleware_wrap import _wrap_with_http_middlewares

            if for_door_mix:
                http_mws = [
                    m
                    for m in http_mws
                    if not getattr(m, "_fastapi_turbo_is_asgi_shim", False)
                ]
            if http_mws:
                handler = _wrap_with_http_middlewares(handler, http_mws, self)
                try:
                    handler._has_http_middleware = True
                except (AttributeError, TypeError):
                    pass

        try:
            handler._fastapi_turbo_route_obj = route
        except (AttributeError, TypeError):
            pass

        params = [
            ParamInfo(
                name="request",
                kind="inject_request",
                type_hint="any",
                required=False,
                default_value=None,
                has_default=False,
                model_class=None,
                alias=None,
                is_handler_param=True,
                scalar_validator=None,
            )
        ]
        return params, handler, False

    def _build_server_args(self, host: str, port: int, for_door: bool = False) -> tuple:
        """Build the full positional argument tuple for ``run_server`` and
        ``register_app_router`` from the app's routes + config, so BOTH
        request doors (the ``app.run()`` socket server and the in-process
        ASGI ``oneshot`` door) drive a byte-identical router. Registers the
        dynamic ``/openapi.json`` route and renders docs HTML, but runs NO
        lifespan/startup handlers — those stay in ``run()`` / the ASGI
        lifespan protocol.

        ``for_door=True`` + a mixed Tower+raw-ASGI middleware app (H2):
        register handlers WITHOUT the raw-ASGI shims and with NO Tower Rust
        layers (``middleware_config=[]``); the door composes the full
        Tower+raw chain itself in registration order via
        ``_asgi_oneshot_http_with_mw``. For every other app this is a no-op
        (the door and ``app.run()`` build identical args)."""
        from fastapi_turbo._fastapi_turbo_core import ParamInfo, RouteInfo

        # Only the mixed Tower+raw-MW case re-routes middleware through the
        # door's outer ASGI chain; pure-Tower / pure-raw / no-MW apps keep the
        # default (byte-identical to ``app.run()``) build.
        _use_door_eps = for_door and self._door_has_tower_raw_mix()

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
            # Drop any existing dynamic route from a prior ``app.run()`` or
            # in-process ASGI dispatch. Docs/redoc/oauth handlers are baked
            # into ``run_server`` below; leaving their Python route entries in
            # the router makes the Rust method router see duplicate /docs.
            def _is_prior_dynamic(r, ep_name, path_val):
                ep = getattr(r, "endpoint", None)
                return (
                    getattr(r, "_fastapi_turbo_dynamic_route", False)
                    and ep is not None
                    and getattr(ep, "__name__", None) == ep_name
                    and getattr(r, "path", None) == path_val
                )
            dynamic_routes = [
                ("openapi", _openapi_url_val),
                ("swagger_ui", self.docs_url),
                ("redoc", self.redoc_url),
                ("swagger_ui_redirect", self.swagger_ui_oauth2_redirect_url),
            ]
            self.router.routes = [
                r for r in self.router.routes
                if not any(
                    path_val and _is_prior_dynamic(r, ep_name, path_val)
                    for ep_name, path_val in dynamic_routes
                )
            ]
            _openapi_route = APIRoute(
                _openapi_url_val,
                _openapi_dynamic,
                methods=["GET"],
                include_in_schema=False,
            )
            _openapi_route._fastapi_turbo_dynamic_route = True
            # Bypass app/router dependencies — docs shouldn't require
            # user-level auth headers.
            _openapi_route._fastapi_turbo_bypass_deps = True
            self.router.routes.insert(0, _openapi_route)

        route_dicts = self._collect_all_routes()
        route_infos: list[RouteInfo] = []

        for rd in route_dicts:
            # Stage D: when the adapter (opt-in) can drive this route off real
            # FastAPI introspection, use its ParamInfo + handler directly.
            _adapted = self._adapter_route_info(rd, for_door_mix=_use_door_eps)
            if _adapted is None:
                # The lean adapter declined — try full delegation to real FastAPI's
                # route handler (default-on) before the clone path.
                _adapted = self._delegated_route_info(rd, for_door_mix=_use_door_eps)
            if (
                _adapted is None
                and not rd.get("is_websocket")
                and rd.get("_route_obj") is not None
            ):
                # End-state safety net: a normal HTTP route (incl. mounted
                # sub-FastAPI routes) NEVER falls to the deleted clone-compiled
                # path — FORCE real-FastAPI delegation, skipping the narrow
                # proving declines. rds WITHOUT a _route_obj (raw-ASGI mount
                # proxies, custom-route/passthrough endpoints, WS) carry
                # self-contained handlers and register below as before.
                _adapted = self._delegated_route_info(
                    rd, for_door_mix=_use_door_eps, force=True
                )
            _trace_path = os.environ.get("FASTAPI_TURBO_TRACE_CLONE")
            if _adapted is None and _trace_path:
                # Clone-deletion telemetry: name every route that still falls back
                # to the clone-compiled handler (both adapter+delegation declined).
                # Appends to the file named by the env var (stderr is swallowed by
                # pytest's per-test capture for passing tests).
                try:
                    with open(_trace_path, "a") as _tf:
                        _tf.write(
                            f"{sorted(rd.get('methods') or [])} {rd.get('path')} "
                            f"ws={bool(rd.get('is_websocket'))} "
                            f"mount={bool(rd.get('_from_mount'))} "
                            f"route_obj={'y' if rd.get('_route_obj') is not None else 'n'}\n"
                        )
                except OSError:
                    pass
            if _adapted is not None:
                _ap, _ah, _aasync = _adapted
                # Stamp the real FastAPI route so the Rust bridge can expose
                # ``request.scope["route"]`` (handler route introspection) AND
                # resolve the route pattern for the per-request scope — which
                # refines the Sentry transaction name from URL- to route-source
                # on the door path (route_obj is otherwise None for adapter
                # routes). See set_request_scope_ctxvar (router.rs).
                _rt_obj = rd.get("_route_obj")
                if _rt_obj is not None:
                    try:
                        _ah._fastapi_turbo_route_obj = _rt_obj
                    except (AttributeError, TypeError):
                        pass
                # The door reads ``_fastapi_turbo_lax_content_type`` off the
                # RouteInfo.handler; collection resolves the strict cascade into
                # rd["_lax_content_type"], so copy it onto whichever handler
                # (adapter / delegated) serves the route (else a lax route with
                # a body validator wrongly 422s a no-Content-Type body).
                if rd.get("_lax_content_type") or getattr(
                    rd.get("endpoint"), "_fastapi_turbo_lax_content_type", False
                ):
                    try:
                        _ah._fastapi_turbo_lax_content_type = True
                    except (AttributeError, TypeError):
                        pass
                route_infos.append(
                    RouteInfo(
                        path=rd["path"],
                        methods=rd["methods"],
                        handler=_ah,
                        is_async=_aasync,
                        handler_name=rd["handler_name"],
                        params=_ap,
                        is_websocket=False,
                        status_code=rd.get("status_code"),
                    )
                )
                continue
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

            _handler = rd["endpoint"]
            if _use_door_eps and rd.get("_endpoint_door") is not None:
                _handler = rd["_endpoint_door"]
            route_infos.append(
                RouteInfo(
                    path=rd["path"],
                    methods=rd["methods"],
                    handler=_handler,
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

        # Door mixed-MW path composes Tower as part of its outer ASGI chain,
        # so DON'T also apply it as Rust router layers (it'd run twice).
        middleware_config = [] if _use_door_eps else self._build_middleware_config()

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
            from fastapi_turbo.requests import _door_make_request
            # Normalize headers to list[(bytes, bytes)] for ASGI scope.
            hdr_list = []
            for k, v in headers or []:
                if isinstance(k, str):
                    k = k.encode("latin-1")
                if isinstance(v, str):
                    v = v.encode("latin-1")
                hdr_list.append((k, v))
            qs = query if isinstance(query, bytes) else (query or "").encode()
            return _door_make_request({
                "type": "http",
                "method": method,
                "path": path,
                "headers": hdr_list,
                "query_string": qs,
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
            # with our synthetic handler as the innermost call. In the door
            # mixed-MW case the raw-ASGI shims are handled by the outer ASGI
            # chain (the 404 response flows back out through it), so strip them
            # here to avoid double-application — @app.middleware/BaseHTTP stay.
            _nf_mws = _app_self._http_middlewares
            if _use_door_eps:
                _nf_mws = [
                    m
                    for m in _nf_mws
                    if not getattr(m, "_fastapi_turbo_is_asgi_shim", False)
                ]
            middlewares = list(reversed(_nf_mws))

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
        from fastapi_turbo.exceptions import (
            RequestValidationError as _RVE,
            _DoorRequestValidationError as _DRVE,
        )
        if _RVE in self.exception_handlers:
            from fastapi_turbo.requests import _door_make_request as _Req
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
                # FA parity: carry endpoint_ctx (function/file/line/path) so a
                # user RequestValidationError handler can log which endpoint the
                # bad request hit, and ``str(exc)`` renders ``in <function>``.
                # The matched endpoint is stamped on the per-request scope by
                # the Rust bridge (set_request_scope_ctxvar) before validation.
                _ep_ctx = None
                try:
                    _scope = _current_request_scope.get() or {}
                    _ep = _scope.get("endpoint")
                    if _ep is not None:
                        from fastapi_turbo._introspect_from_real_fastapi import (
                            endpoint_ctx_for as _ep_ctx_for,
                        )
                        _rt = _scope.get("route")
                        _ep_ctx = _ep_ctx_for(_ep, getattr(_rt, "path", None))
                except Exception:
                    _ep_ctx = None
                exc = _DRVE(errors_list, body=_body_for_rve, endpoint_ctx=_ep_ctx)
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
                import importlib as _importlib
                _docs_mod = _importlib.import_module("fastapi.openapi.docs")
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
                import importlib as _importlib
                _docs_mod = _importlib.import_module("fastapi.openapi.docs")
                if _docs_mod is not None:
                    resp = _docs_mod.get_redoc_html(
                        openapi_url=self.openapi_url,
                        title=self.title + " - ReDoc",
                    )
                    redoc_html_str = resp.body.decode("utf-8") if hasattr(resp, "body") else None
            except Exception as _exc:  # noqa: BLE001
                _log.debug("silent catch in applications: %r", _exc)
                redoc_html_str = None

        # Static-file Content-Type map derived from Python's ``mimetypes`` —
        # Starlette's source of truth (``StaticFiles`` → ``FileResponse`` →
        # ``guess_type(name)[0] or "text/plain"``, then ``; charset=utf-8`` iff
        # the type is ``text/*``). A hardcoded Rust table / ``mime_guess``
        # cannot match this across Python versions + OS mime files (``.js`` is
        # ``text/javascript`` on 3.12+ but ``application/javascript`` on
        # 3.10/3.11; ``.yaml`` / ``.xml`` / ``.otf`` also differ), so we compute
        # the ext→content-type map here and hand it to Rust. Keys are lowercase
        # extensions WITHOUT the dot. Bound unconditionally so the run_server
        # call below always has it (empty when no static mounts).
        _static_content_types: list[tuple[str, str]] = []
        if static_mounts:
            import mimetypes as _mimetypes
            _mimetypes.init()
            _ct_by_ext: dict[str, str] = {}
            for _ext, _mtype in _mimetypes.types_map.items():
                _e = _ext.lstrip(".").lower()
                if not _e:
                    continue
                _ct_by_ext[_e] = (
                    f"{_mtype}; charset=utf-8"
                    if _mtype.startswith("text/")
                    else _mtype
                )
            _static_content_types = list(_ct_by_ext.items())

        return (
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
            _static_content_types,
        )

    # ------------------------------------------------------------------
    # ASGI __call__ — enables ``uvicorn myapp:app`` compatibility
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # In-process ASGI door (door B): drive the SAME assembled axum::Router
    # via tower::Service::oneshot — no socket, no double-hop. DEFAULT-ON and
    # the sole in-process engine for HTTP + WebSocket; the Python dispatchers
    # it replaced are deleted (Phase 7). FASTAPI_TURBO_ONESHOT_DOOR=0 opts out
    # to the loopback Rust socket server.
    # ------------------------------------------------------------------

    def _oneshot_door_enabled(self) -> bool:
        """The oneshot door is THE Rust HTTP engine for the ASGI path (uvicorn
        / TestClient in-process / httpx.ASGITransport) — DEFAULT-ON, and the
        only in-process HTTP engine now that the Python dispatcher is deleted
        (Phase 7). Opt OUT via ``FASTAPI_TURBO_ONESHOT_DOOR=0`` to fall back to
        the loopback Rust socket server (``app.run()``-style) — note that needs
        to bind a port, so it won't work in socket-restricted sandboxes (the
        door does). See CLONE_DELETION_PLAN.md."""
        return os.environ.get("FASTAPI_TURBO_ONESHOT_DOOR", "1") != "0"

    def _door_has_tower_raw_mix(self) -> bool:
        """True when the app registered BOTH a raw-ASGI middleware and a
        Tower-bound marker (CORS/GZip/HTTPSRedirect). That's the one case
        where the door's default split — Tower as Rust router layers
        (outer), raw-ASGI as inner per-handler shims — can't reproduce
        Starlette's registration order (a raw MW added AFTER an
        HTTPSRedirect must wrap its 307). For the mix, the door instead
        composes ALL Tower+raw MW as one OUTER ASGI chain
        (``_door_outer_mw_list``) around handlers built WITHOUT the raw
        shims and WITHOUT Tower layers (``_build_server_args(for_door=
        True)``). Pure-Tower / pure-raw / no-MW apps keep the default
        (byte-identical) path."""
        if not getattr(self, "_raw_asgi_middlewares", None):
            return False
        return any(
            _k == "tower"
            for (_k, *_rest) in (getattr(self, "_mw_registration_log", None) or [])
        )

    def _door_outer_mw_list(self) -> list:
        """Registration-ordered ``[(real_asgi_class, kwargs), ...]`` for the
        Tower-bound + raw-ASGI middlewares, used to compose the door's outer
        ASGI chain for the mixed-MW case. Empty unless
        ``_door_has_tower_raw_mix()``. Mirrors the (now-removed) Python
        dispatcher's composer: Tower markers are inert as ASGI on their own,
        so they're substituted with the real Starlette class; raw-ASGI
        classes are user-provided and used as-is. ``@app.middleware`` /
        ``BaseHTTPMiddleware`` are NOT here — they stay inner (baked into the
        handler), matching the dispatcher."""
        if not self._door_has_tower_raw_mix():
            return []
        out: list = []
        for kind, mw_cls, mw_kwargs, _seq in (
            getattr(self, "_mw_registration_log", None) or []
        ):
            if kind == "tower":
                resolved = _resolve_tower_bound_to_asgi_class(mw_cls)
                if resolved is not None:
                    out.append((resolved, mw_kwargs))
            elif kind == "raw":
                out.append((mw_cls, mw_kwargs))
        return out

    def _oneshot_needs_disconnect_watch(self) -> bool:
        """True when any route endpoint streams (constructs a
        ``StreamingResponse`` / ``FileResponse`` / ``EventSourceResponse``) or
        polls ``request.is_disconnected()`` (SSE / long-poll), so
        ``_asgi_oneshot_http`` wires a disconnect ``Event`` + receive-poller —
        an SSE poll then observes the drop AND an otherwise-infinite stream is
        cancelled (the door drops the body stream → the generator's
        ``GeneratorExit`` cleanup runs). Static, AST-free, best-effort (False on
        inspection failure). Cached — routes are fixed after startup."""
        cached = getattr(self, "_oneshot_disconnect_watch", None)
        if cached is not None:
            return cached
        import inspect as _inspect

        _WATCH_MARKERS = (
            "is_disconnected",
            "StreamingResponse",
            "FileResponse",
            "EventSourceResponse",
        )
        needs_watch = False
        for route in getattr(getattr(self, "router", None), "routes", None) or ():
            endpoint = getattr(route, "endpoint", None)
            if endpoint is None:
                continue
            # Bare generator endpoints (door streams them as NDJSON) keep the
            # prior behaviour of not arming the watch from their own source.
            if _inspect.isasyncgenfunction(endpoint) or _inspect.isgeneratorfunction(
                endpoint
            ):
                continue
            try:
                src = _inspect.getsource(endpoint)
            except (OSError, TypeError):
                continue
            if any(m in src for m in _WATCH_MARKERS):
                needs_watch = True
                break
        self._oneshot_disconnect_watch = needs_watch
        return needs_watch

    def _door_fingerprint(self) -> tuple:
        """Cheap structural fingerprint of the routes + middleware that the
        door's Rust router and WS table are built from. Used to detect routes /
        middleware / mounts added AFTER the first in-process request (e.g. lazy
        ``app.mount`` / plugin registration) so the door re-registers instead of
        serving a stale router (or 1000-closing a freshly-added WS route)."""
        router = getattr(self, "router", None)
        # dependency_overrides are a testing feature set at RUNTIME (after the door
        # registered). Include the current override key-set so the door re-registers
        # when it changes — override-active routes then decline to the clone path
        # (which resolves overrides per request, incl. different-signature ones).
        _ov = getattr(self, "dependency_overrides", None)
        # The door caches a per-route ``wants_request_scope`` flag (skips the
        # request-scope ctxvar set when no consumer exists). Its inputs are the
        # exception-handler count and the Sentry-installed flag; include both so
        # a handler registered AFTER the first request forces a RouteState
        # rebuild instead of leaving the scope permanently un-populated.
        return (
            len(getattr(router, "routes", None) or ()),
            len(getattr(self, "_http_middlewares", None) or ()),
            len(getattr(self, "_middleware_stack", None) or ()),
            len(getattr(self, "_raw_asgi_middlewares", None) or ()),
            len(getattr(self, "_mounts", None) or ()),
            frozenset(id(k) for k in _ov) if _ov else None,
            bool(getattr(self, "exception_handlers", None)),
            bool(getattr(self, "_fastapi_turbo_sentry_installed", False)),
        )

    def _ensure_oneshot_registered(self, scope: dict) -> None:
        """Register the assembled router for this app, using the same
        full-fidelity args as ``app.run()``. Re-registers when the route /
        middleware fingerprint changes (routes added after the first request),
        so the door never serves a stale router."""
        fp = self._door_fingerprint()
        if (
            getattr(self, "_oneshot_registered", False)
            and getattr(self, "_oneshot_reg_fingerprint", None) == fp
        ):
            return
        from fastapi_turbo._fastapi_turbo_core import register_app_router

        server = scope.get("server") or ("127.0.0.1", 0)
        host = str(server[0] or "127.0.0.1")
        port = int(server[1] or 0)
        register_app_router(id(self), *self._build_server_args(host, port, for_door=True))
        self._oneshot_registered = True
        # Store the fingerprint AFTER ``_build_server_args`` — it normalises the
        # dynamic ``/openapi.json`` route into ``router.routes``, so capturing
        # it post-registration keeps the value stable across later requests
        # (otherwise every request would see a changed count and re-register).
        self._oneshot_reg_fingerprint = self._door_fingerprint()
        # A structural change also invalidates the cached WS route table and
        # the disconnect-watch scan, which are derived from the same routes.
        self._ws_door_route_table = None
        self._oneshot_disconnect_watch = None

    def _oneshot_mutate_outer_scope(self, scope: dict) -> None:
        """Populate ``scope['route']`` / ``scope['path_params']`` /
        ``scope['endpoint']`` on the OUTER ASGI scope, mirroring Starlette's
        in-place router mutation. Outer ASGI middleware that wraps the
        whole app (legacy ``SentryAsgiMiddleware(app)``, OTel, rate-limit)
        reads ``scope['route'].path`` at response-start time to template
        the transaction name — without this it records the concrete path
        (``/message/123456``) instead of the route shape
        (``/message/{message_id}``). R25 regression: the oneshot door
        dispatches entirely in Rust and never touched the outer scope.

        Best-effort: a lightweight regex match reusing the per-route
        ``_fastapi_turbo_asgi_regex`` cache the dispatcher already
        populates. Any failure is swallowed — the scope decoration is a
        compatibility nicety, never load-bearing for the response."""
        import re as _re

        try:
            method = scope.get("method", "GET").upper()
            path = scope.get("path", "/")
            router = getattr(self, "router", None)
            for route in getattr(router, "routes", None) or ():
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
                r_methods = {
                    m.upper() for m in (getattr(route, "methods", None) or ())
                }
                if method not in r_methods:
                    continue
                scope["route"] = route
                scope["path_params"] = match.groupdict()
                scope["endpoint"] = getattr(route, "endpoint", None)
                return
        except Exception as _exc:  # noqa: BLE001
            _log.debug("oneshot outer-scope decoration skipped: %r", _exc)

    async def _asgi_try_http_mount(
        self, scope: dict, receive: Callable, send: Callable
    ) -> bool:
        """In-process HTTP mount dispatch for the door path. If the request
        path falls under a registered ``app.mount(prefix, subapp)`` or a
        Starlette ``Mount`` route (and no top-level literal route shadows it),
        strip the prefix and recurse into the sub-app's ASGI ``__call__`` —
        all in-process, no dispatcher and no loopback socket. Returns True
        when a mount handled the request. Mirrors the (now-legacy) dispatcher
        Mount-dispatch block so mounts keep working once the door is the only
        ASGI engine."""
        path = scope.get("path", "/")
        method = scope.get("method", "GET")
        # app.mount(prefix, subapp)
        for mount_path, mounted_app, _mname in getattr(self, "_mounts", []) or []:
            prefix = (mount_path or "").rstrip("/")
            if not prefix:
                continue  # root mount: defer to the normal matcher
            if path == prefix or path.startswith(prefix + "/"):
                top_level_hit = any(
                    getattr(r, "path", None) == path
                    and method in {m.upper() for m in (getattr(r, "methods", None) or ())}
                    for r in self.router.routes
                )
                if top_level_hit:
                    continue
                sub_path = path[len(prefix):] or "/"
                sub_scope = dict(scope)
                sub_scope["path"] = sub_path
                sub_scope["raw_path"] = sub_path.encode("latin-1")
                sub_scope["root_path"] = scope.get("root_path", "") + prefix
                # APIRouter FIRST: it is now a real Starlette Router
                # subclass (callable as ASGI), but turbo routers must be
                # served through a wrapping app (deferred include + AES
                # scopes), not raw Router.__call__.
                from fastapi_turbo.routing import APIRouter as _APIRouter
                if isinstance(mounted_app, _APIRouter):
                    sub_app = type(self)()
                    try:
                        sub_app.include_router(mounted_app)
                    except Exception as _exc:  # noqa: BLE001
                        _log.debug("in-process APIRouter mount: %r", _exc)
                    await sub_app(sub_scope, receive, send)
                    return True
                if callable(mounted_app):
                    await mounted_app(sub_scope, receive, send)
                    return True
        # Starlette Mount routes declared via FastAPI(routes=[Mount(...)])
        for route in getattr(self.router, "routes", []) or []:
            if not _looks_like_starlette_mount(route):
                continue
            prefix = (getattr(route, "path", "") or "").rstrip("/")
            if not prefix:
                continue
            if not (path == prefix or path.startswith(prefix + "/")):
                continue
            top_level_hit = any(
                r is not route
                and not _looks_like_starlette_mount(r)
                and getattr(r, "path", None) == path
                and method in {m.upper() for m in (getattr(r, "methods", None) or ())}
                for r in self.router.routes
            )
            if top_level_hit:
                continue
            mounted_app = _mounted_route_asgi_app(type(self), route)
            if mounted_app is None:
                continue
            sub_path = path[len(prefix):] or "/"
            sub_scope = dict(scope)
            sub_scope["path"] = sub_path
            sub_scope["raw_path"] = sub_path.encode("latin-1")
            sub_scope["root_path"] = scope.get("root_path", "") + prefix
            # APIRouter FIRST (see the _mounts branch above): turbo
            # routers are real Starlette Routers now, hence callable.
            from fastapi_turbo.routing import APIRouter as _APIRouter
            if isinstance(mounted_app, _APIRouter):
                sub_app = type(self)(docs_url=None, redoc_url=None, openapi_url=None)
                sub_app.include_router(mounted_app)
                await sub_app(sub_scope, receive, send)
                return True
            if callable(mounted_app):
                await mounted_app(sub_scope, receive, send)
                return True
        return False

    async def _asgi_oneshot_http_with_mw(
        self, scope: dict, receive: Callable, send: Callable
    ) -> bool:
        """Drive the oneshot door, composing the Tower-bound + raw-ASGI
        middlewares as an OUTER ASGI chain in registration order (last
        registered = outermost) so they wrap the Rust-produced response —
        INCLUDING a short-circuit (e.g. an HTTPSRedirect 307) — exactly like
        the (removed) Python dispatcher. Active only for the mixed Tower+raw
        case (``_door_outer_mw_list`` is empty otherwise); the door's
        registered handlers are then built WITHOUT the raw-ASGI shims and
        WITHOUT Tower Rust layers (``_build_server_args(for_door=True)``), so
        nothing double-applies. @app.middleware / BaseHTTPMiddleware stay
        inner (baked into the handler)."""
        outer_mws = self._door_outer_mw_list()
        if not outer_mws:
            return await self._asgi_oneshot_http(scope, receive, send)

        async def _leaf(inner_scope, inner_receive, inner_send):
            # ``_asgi_oneshot_http`` drives process_request_streaming and emits
            # the status/headers/body ASGI frames, so the outer chain can
            # observe and decorate them. It always handles HTTP (returns True).
            await self._asgi_oneshot_http(inner_scope, inner_receive, inner_send)

        composed = _leaf
        # Forward order: ``add_middleware(X)`` then ``add_middleware(Y)`` ⇒ Y
        # outermost. forward-wrap gives ``Y(X(leaf))`` so Y.__call__ runs first.
        for mw_cls, mw_kwargs in outer_mws:
            try:
                composed = mw_cls(app=composed, **mw_kwargs)
            except TypeError:
                composed = mw_cls(**mw_kwargs)
        await composed(scope, receive, send)
        return True

    async def _asgi_oneshot_http(self, scope: dict, receive: Callable, send: Callable) -> bool:
        """Drive one HTTP request through the Rust engine in-process via
        ``process_request_streaming`` and emit the ASGI response (status +
        headers, then the body streamed chunk-by-chunk). Returns True when
        handled. The blocking dispatch runs in a thread so the asyncio loop is
        not blocked (the GIL is released inside the Rust call)."""
        import asyncio

        from fastapi_turbo._fastapi_turbo_core import process_request_streaming

        self._ensure_oneshot_registered(scope)
        # Decorate the outer scope with the matched route shape so
        # app-wrapping ASGI middleware (Sentry/OTel) sees it — same as
        # the dispatcher / Starlette's router. R25 parity.
        self._oneshot_mutate_outer_scope(scope)

        # Drain the request body from the ASGI ``receive`` channel.
        body = b""
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] == "http.disconnect":
                return True  # client gone before we could respond
            body += message.get("body", b"") or b""
            more_body = message.get("more_body", False)

        method = scope.get("method", "GET")
        path = scope.get("path", "/")
        query_string = scope.get("query_string", b"") or b""
        if isinstance(query_string, (bytes, bytearray)):
            query_string = bytes(query_string).decode("latin-1")
        headers = [(bytes(k), bytes(v)) for k, v in scope.get("headers", [])]
        client = scope.get("client") or ("127.0.0.1", 0)
        client_host = str(client[0] or "127.0.0.1")
        client_port = int(client[1] or 0)

        # Disconnect signal for handlers that poll ``request.is_disconnected()``
        # (SSE / long-poll). The Rust request has no live ASGI receive, so hand
        # it a ``threading.Event`` and set it from a background receive-poller —
        # only for apps that actually poll (no per-request overhead otherwise).
        disconnect_event = None
        disconnect_task = None
        if self._oneshot_needs_disconnect_watch():
            import threading

            disconnect_event = threading.Event()

            async def _watch_disconnect():
                try:
                    while True:
                        m = await receive()
                        if m.get("type") == "http.disconnect":
                            disconnect_event.set()
                            return
                except Exception:  # noqa: BLE001
                    pass

            disconnect_task = asyncio.ensure_future(_watch_disconnect())

        loop = asyncio.get_running_loop()
        status, resp_headers, body_stream = await loop.run_in_executor(
            None,
            process_request_streaming,
            id(self),
            method,
            path,
            query_string,
            headers,
            body,
            client_host,
            client_port,
            disconnect_event,
        )

        # Bodiless statuses (1xx, 204 No Content, 304 Not Modified) MUST
        # NOT carry a Content-Length per RFC 7230 §3.3.2. The assembled
        # axum router emits ``content-length: 0`` for a bodiless 204
        # (hyper would strip it at wire-serialize time, but the oneshot
        # door bypasses hyper and returns headers verbatim). The Python
        # dispatcher + upstream Starlette omit it, so strip it here to
        # keep parity. ``GET /no-content`` returned ``content-length: 0``
        # via the door vs nothing upstream (R55 204-parity finding).
        out_headers = [[bytes(k), bytes(v)] for k, v in resp_headers]
        if status < 200 or status in (204, 304):
            out_headers = [
                (k, v)
                for k, v in out_headers
                if k.lower() != b"content-length"
            ]
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": out_headers,
            }
        )
        # Pump body chunks lazily off Axum's BodyDataStream. For a buffered
        # response this is a single chunk; for a StreamingResponse / SSE it
        # streams frame-by-frame with no 32 MiB cap and no hang on large or
        # infinite bodies. ``next_chunk`` blocks on the shared oneshot runtime
        # inside the executor so the asyncio loop stays free.
        disconnected = False
        try:
            while True:
                # Client gone? Stop draining and DROP the body stream so the
                # server-side generator is cancelled (GeneratorExit) — otherwise
                # an infinite stream would run forever.
                if disconnect_event is not None and disconnect_event.is_set():
                    await loop.run_in_executor(None, body_stream.close)
                    disconnected = True
                    break
                chunk = await loop.run_in_executor(None, body_stream.next_chunk)
                if chunk is None:
                    break
                try:
                    await send(
                        {
                            "type": "http.response.body",
                            "body": bytes(chunk),
                            "more_body": True,
                        }
                    )
                except Exception:  # noqa: BLE001
                    # Send failed mid-stream (client dropped) — cancel the generator.
                    await loop.run_in_executor(None, body_stream.close)
                    disconnected = True
                    break
            if not disconnected:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            if disconnect_task is not None:
                disconnect_task.cancel()

        # Re-raise an unhandled server exception AFTER the 500 response is
        # sent (Starlette ServerErrorMiddleware semantics).
        # The Rust dispatch core renders the 500 (running any catch-all
        # ``@app.exception_handler(Exception)``) and records the original
        # exception on ``_captured_server_exceptions`` — via the compiled
        # handler wrapper for the with-handler case, and via
        # ``pyerr_to_response`` (responses.rs) for the no-handler case.
        # Re-raising it here propagates it out of ``__call__`` so
        # ``httpx.ASGITransport(raise_app_exceptions=True)`` /
        # ``TestClient(raise_server_exceptions=True)`` surface the original
        # exception; under ``raise_app_exceptions=False`` httpx swallows the
        # raise and the rendered 500 response is what the caller observes.
        # Drain (pop+clear) so the list is empty for the next request and
        # TestClient's ``_check_raised`` doesn't double-raise.
        captured = getattr(self, "_captured_server_exceptions", None)
        if captured:
            exc = captured.pop(0)
            captured.clear()
            raise exc
        return True

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        """ASGI entry point.

        Dispatch rules:
          * ``lifespan``   → drive startup/shutdown handlers directly.
          * ``http``       → the in-process Rust oneshot door
                             (``_asgi_oneshot_http_with_mw`` →
                             ``process_request_streaming``) serves the request
                             in-process — no socket needed, so the app works
                             under ``httpx.ASGITransport(app=app)`` / hermetic
                             test runners. ``FASTAPI_TURBO_ONESHOT_DOOR=0`` (or
                             a ``_fastapi_turbo_force_proxy`` scope flag) opts
                             out to the loopback Rust socket server.
          * ``websocket``  → the in-process WebSocket door (``_asgi_ws_door``)
                             drives a Python ``WebSocket`` over the ASGI
                             receive/send channels, reusing the same wrapped
                             endpoint ``app.run()`` uses; same opt-out applies.
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

            # In-process HTTP mount dispatch (door path): recurse into mounted
            # sub-apps with the prefix stripped, before the door tries the
            # assembled router (which doesn't host mounts). Keeps mounts on the
            # in-process path — no dispatcher, no loopback socket.
            if await self._asgi_try_http_mount(scope, receive, send):
                return

            # In-process oneshot door — THE Rust HTTP engine for the ASGI
            # path (uvicorn / TestClient in-process / httpx.ASGITransport). It
            # hosts the entire HTTP surface: params/body/deps/validation,
            # streaming + client-disconnect, mounts, and the full middleware
            # surface including mixed Tower+raw-ASGI (_asgi_oneshot_http_with_mw
            # composes them as one registration-ordered chain). The ~3.3K-line
            # Python in-process dispatcher this replaced has been DELETED
            # (Phase 7). ``_fastapi_turbo_force_proxy`` (scope) or
            # ``FASTAPI_TURBO_ONESHOT_DOOR=0`` opt OUT to the loopback Rust
            # socket server.
            force_proxy = bool(scope.get("_fastapi_turbo_force_proxy"))
            if not force_proxy and self._oneshot_door_enabled():
                if await self._asgi_oneshot_http_with_mw(scope, receive, send):
                    return
            # Door opted out (or forced proxy) — fall back to the loopback
            # Rust server (real socket).
            await self._asgi_ensure_server()
            await self._asgi_proxy_http(scope, receive, send)
            return

        if scope["type"] == "websocket":
            # WS mount handling: when a sub-app is mounted at
            # ``/sub`` and the client opens ``/sub/ws/...``, strip
            # the prefix and forward to the mounted app's ``__call__``
            # with ``scope['root_path']`` updated. Mirrors the HTTP
            # path's mount routing so sub-apps' WS endpoints work.
            ws_path_local = scope.get("path", "/")
            for _mp, _ma, _mn in getattr(self, "_mounts", []) or []:
                _mp_strip = (_mp or "").rstrip("/")
                if not _mp_strip:
                    continue
                if (
                    ws_path_local == _mp_strip
                    or ws_path_local.startswith(_mp_strip + "/")
                ):
                    if not callable(_ma) or isinstance(_ma, APIRouter):
                        # Mounted bare APIRouters are callable now (real
                        # Starlette Router) but their WS routes are
                        # WSRoute holders the raw Router can't dispatch —
                        # fall through to the in-process WS door.
                        continue
                    sub_ws_scope = dict(scope)
                    sub_ws_path = ws_path_local[len(_mp_strip):] or "/"
                    sub_ws_scope["path"] = sub_ws_path
                    sub_ws_scope["raw_path"] = sub_ws_path.encode("latin-1")
                    sub_ws_scope["root_path"] = (
                        scope.get("root_path", "") + _mp_strip
                    )
                    await _ma(sub_ws_scope, receive, send)
                    return

            for _route in getattr(self.router, "routes", []) or []:
                if not _looks_like_starlette_mount(_route):
                    continue
                _mp_strip = (getattr(_route, "path", "") or "").rstrip("/")
                if not _mp_strip:
                    continue
                if not (
                    ws_path_local == _mp_strip
                    or ws_path_local.startswith(_mp_strip + "/")
                ):
                    continue
                _ma = _mounted_route_asgi_app(type(self), _route)
                if not callable(_ma) or isinstance(_ma, APIRouter):
                    continue
                sub_ws_scope = dict(scope)
                sub_ws_path = ws_path_local[len(_mp_strip):] or "/"
                sub_ws_scope["path"] = sub_ws_path
                sub_ws_scope["raw_path"] = sub_ws_path.encode("latin-1")
                sub_ws_scope["root_path"] = scope.get("root_path", "") + _mp_strip
                await _ma(sub_ws_scope, receive, send)
                return

            # In-process WebSocket door: route-match against the app's WS
            # routes and drive the SAME wrapped endpoint ``app.run()`` uses
            # (``_wrap_websocket_endpoint`` — deps / validation / WS- and
            # raw-ASGI-middleware / exception routing) over a Python
            # ``WebSocket`` backed by the ASGI receive/send channels. The
            # caller's event loop already drives everything, so no loopback
            # socket and no Rust channel bridge are needed. WS middleware and
            # raw-ASGI middleware are applied INSIDE the wrapped endpoint, so
            # we don't wrap them here. ``_fastapi_turbo_force_proxy`` (or
            # ``FASTAPI_TURBO_ONESHOT_DOOR=0``) opts OUT to the loopback Rust
            # WS server.
            force_proxy = bool(scope.get("_fastapi_turbo_force_proxy"))
            if not force_proxy and self._oneshot_door_enabled():
                if await self._asgi_ws_door(scope, receive, send):
                    return
            await self._asgi_ensure_server()
            await self._asgi_proxy_websocket(scope, receive, send)
            return

    def _ws_door_table(self) -> list:
        """Cached ``[(regex, wrapped_endpoint, route_path, route_obj), ...]``
        for the app's WebSocket routes. ``wrapped_endpoint`` is the SAME
        ``_wrap_websocket_endpoint`` output the Rust ``app.run()`` door
        registers (built by ``_collect_all_routes`` with the full effective
        dependency set), so the in-process door applies identical
        deps/validation/middleware/exception handling. Cached, but rebuilt when
        the route/middleware fingerprint changes (a WS route added after the
        first WS request would otherwise 1000-close)."""
        fp = self._door_fingerprint()
        cached = getattr(self, "_ws_door_route_table", None)
        if cached is not None and getattr(self, "_ws_door_table_fingerprint", None) == fp:
            return cached
        import re as _re
        table: list = []
        for rd in self._collect_all_routes():
            if not rd.get("is_websocket"):
                continue
            r_path = rd.get("path") or ""
            if not r_path:
                continue
            pattern = "^"
            idx = 0
            for m in _re.finditer(r"\{([^{}:]+)(?::([^{}]+))?\}", r_path):
                pattern += _re.escape(r_path[idx:m.start()])
                pname = m.group(1)
                pattern += (
                    f"(?P<{pname}>.+)" if m.group(2) == "path"
                    else f"(?P<{pname}>[^/]+)"
                )
                idx = m.end()
            pattern += _re.escape(r_path[idx:]) + "$"
            table.append(
                (_re.compile(pattern), rd["endpoint"], r_path, rd.get("_route_obj"))
            )
        self._ws_door_route_table = table
        self._ws_door_table_fingerprint = fp
        return table

    async def _asgi_ws_door(
        self, scope: dict, receive: Callable, send: Callable
    ) -> bool:
        """Serve an ASGI ``websocket`` scope in-process: match a WS route,
        build a Python ``WebSocket`` over the ASGI receive/send channels, and
        drive the shared wrapped endpoint on the caller's event loop. Always
        returns True (handled): on no match it closes with 1000, matching
        Starlette. Replaces the deleted ~900-line Python WS dispatcher."""
        from fastapi_turbo.websockets import WebSocket as _WS

        path = scope.get("path", "/")
        wrapped = None
        route_obj = None
        path_params: dict = {}
        for regex, _wrapped, _route_path, _route_obj in self._ws_door_table():
            m = regex.match(path)
            if m is None:
                continue
            wrapped = _wrapped
            route_obj = _route_obj
            path_params = m.groupdict()
            break

        if wrapped is None:
            # No matching WS route — Starlette closes with 1000 (normal).
            try:
                await send({"type": "websocket.close", "code": 1000})
            except Exception:  # noqa: BLE001
                pass
            return True

        if route_obj is not None:
            scope["route"] = route_obj
        scope["path_params"] = path_params
        scope["app"] = self
        ws = _WS(scope, receive=receive, send=send)
        ws._app = self
        await wrapped(ws, **path_params)
        # Flush a terminal close queued from a SYNC code path (``_reject`` /
        # ``_handle_ws_exc`` can't await); no-ops if already closed.
        pc = getattr(ws, "_asgi_pending_close", None)
        if pc is not None:
            await ws._asgi_send_close(*pc)
        return True

    # ── lifespan ──────────────────────────────────────────────────────

    async def _asgi_lifespan(self, scope: dict, receive: Callable, send: Callable) -> None:
        # IMPORTANT: drive lifespans + startup/shutdown handlers via the
        # ``_async_run_*`` coroutines so they run on the loop awaiting
        # ``__call__`` (uvicorn's loop in production). The sync
        # ``_run_*`` variants submit through ``_async_worker.submit``
        # and bind any asyncio resource the user creates in lifespan
        # (asyncpg pool, ``redis.asyncio`` client, aiohttp session) to
        # the worker thread's loop instead — first request from a
        # handler awaiting that resource then hits "Future attached to
        # a different loop". Issue #1.
        # When the oneshot door is the active HTTP path for this app,
        # async handlers run on the ``_async_worker`` loop (the Rust
        # engine routes a suspending coroutine through
        # ``submit_to_async_worker`` — exactly as under ``app.run()``).
        # So lifespan MUST also run on the worker loop, or an asyncio
        # primitive created at startup (asyncpg pool, ``redis.asyncio``
        # client, ``asyncio.Lock``/``Event``) binds to the caller's
        # loop and the first request that awaits it hits "got Future
        # attached to a different loop". The sync ``_run_*`` helpers
        # submit through ``_async_worker.submit`` (see
        # ``_run_lifespan_startup``), giving the worker-loop affinity
        # the door needs — this mirrors ``app.run()``'s startup path
        # rather than the async-dispatcher's caller-loop path. Issue #1.
        _door_owns_http = self._oneshot_door_enabled()
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    if _door_owns_http:
                        if self._collect_lifespans():
                            self._run_lifespan_startup()
                        self._run_startup_handlers()
                    else:
                        if self._collect_lifespans():
                            await self._async_run_lifespan_startup()
                        await self._async_run_startup_handlers()
                    await send({"type": "lifespan.startup.complete"})
                except Exception as exc:
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
                    return
            elif message["type"] == "lifespan.shutdown":
                try:
                    if _door_owns_http:
                        if getattr(self, "_lifespan_cms", None):
                            self._run_lifespan_shutdown()
                        self._run_shutdown_handlers()
                    else:
                        if getattr(self, "_lifespan_cms", None):
                            await self._async_run_lifespan_shutdown()
                        await self._async_run_shutdown_handlers()
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


# ──────────────────────────────────────────────────────────────────────
# Public-API ``inspect.signature`` parity (R52 finding 6)
#
# Upstream FastAPI's HTTP-method decorators (``FastAPI.get``,
# ``APIRouter.get`` and friends) declare the full path-operation
# kwarg surface explicitly so SDK generators / type-aware tooling
# can introspect the signature. Our methods used ``**kwargs`` for
# brevity — runtime worked, but ``inspect.signature(FastAPI.get)``
# returned ``(self, path, **kwargs)`` and tools like the upstream
# ``generate_clients`` example, IDE intellisense, and Sphinx
# autosummary lost the kwarg names. Build a canonical Signature
# once at module import and attach it via ``__signature__`` on
# every HTTP-method decorator on both classes.
# ──────────────────────────────────────────────────────────────────────


def _install_path_operation_signatures() -> None:
    import inspect as _insp_sig
    from fastapi_turbo.routing import APIRouter as _APIRouter_sig

    # Canonical kwargs accepted by FastAPI / APIRouter HTTP-method
    # decorators. Mirrors upstream FastAPI 0.136.0; deliberately
    # drops the verbose ``Annotated[..., Doc(...)]`` payload — only
    # the parameter NAMES + DEFAULTS are needed for introspection.
    _Param = _insp_sig.Parameter
    _self = _Param("self", _Param.POSITIONAL_OR_KEYWORD)
    _path = _Param(
        "path", _Param.POSITIONAL_OR_KEYWORD, annotation=str,
    )
    _http_kw_specs = [
        ("response_model", None),
        ("status_code", None),
        ("tags", None),
        ("dependencies", None),
        ("summary", None),
        ("description", None),
        ("response_description", "Successful Response"),
        ("responses", None),
        ("deprecated", None),
        ("operation_id", None),
        ("response_model_include", None),
        ("response_model_exclude", None),
        ("response_model_by_alias", True),
        ("response_model_exclude_unset", False),
        ("response_model_exclude_defaults", False),
        ("response_model_exclude_none", False),
        ("include_in_schema", True),
        ("response_class", None),
        ("name", None),
        ("callbacks", None),
        ("openapi_extra", None),
        ("generate_unique_id_function", None),
    ]
    _http_kw = [
        _Param(n, _Param.KEYWORD_ONLY, default=d)
        for n, d in _http_kw_specs
    ]
    _http_sig = _insp_sig.Signature([_self, _path, *_http_kw])
    # ``api_route`` adds a ``methods`` kwarg.
    _api_route_kw = [
        _Param("methods", _Param.KEYWORD_ONLY, default=None),
        *_http_kw,
    ]
    _api_route_sig = _insp_sig.Signature([_self, _path, *_api_route_kw])
    # ``websocket`` takes path + name; no path-operation kwargs.
    _ws_sig = _insp_sig.Signature([
        _self,
        _path,
        _Param("name", _Param.KEYWORD_ONLY, default=None),
    ])

    _http_methods = ("get", "post", "put", "delete", "patch", "options", "head", "trace")
    for _cls in (FastAPI, _APIRouter_sig):
        for _name in _http_methods:
            _fn = getattr(_cls, _name, None)
            if _fn is not None:
                try:
                    _fn.__signature__ = _http_sig  # type: ignore[attr-defined]
                except (AttributeError, TypeError):
                    pass
        _ar = getattr(_cls, "api_route", None)
        if _ar is not None:
            try:
                _ar.__signature__ = _api_route_sig  # type: ignore[attr-defined]
            except (AttributeError, TypeError):
                pass
        _ws = getattr(_cls, "websocket", None)
        if _ws is not None:
            try:
                _ws.__signature__ = _ws_sig  # type: ignore[attr-defined]
            except (AttributeError, TypeError):
                pass


_install_path_operation_signatures()
