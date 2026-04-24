"""Per-handler middleware wrapping.

When a FastAPI app has any HTTP middleware registered (including raw
ASGI3 classes like ``SessionMiddleware`` / ``SentryAsgiMiddleware``),
every route's compiled handler is wrapped in a synchronous middleware
chain driver. This module holds the three pieces of that machinery:

* ``_make_asgi_middleware_shim`` — bridges a raw ASGI3 middleware
  class into the ``async(request, call_next)`` shape that the chain
  driver understands. Tracks whether the middleware invoked the
  inner app so Sentry can name the transaction after the middleware
  class when it rejects the request (TrustedHost returning 400,
  auth middleware returning 401, etc.).
* ``_wrap_with_http_middlewares`` — builds the synchronous chain
  runner around an already-compiled endpoint. The fast path drives
  each middleware coroutine via ``coro.send(None)`` on the current
  thread. Falls back to ``_drive_async_fallback`` if any middleware
  suspends on real asyncio I/O.
* ``_drive_async_fallback`` — when ``send(None)`` raises
  ``_MiddlewareSuspendedError``, run the full chain through a fresh
  asyncio event loop so real awaits complete.

Splitting these out of ``applications.py`` keeps the core dispatch
file smaller and documents the middleware contract in one place.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

from fastapi_turbo._sentry_compat import (
    _current_request_scope,
    _maybe_install_sentry_request_event_processor,
    _maybe_sentry_capture_failed_request,
    _refine_sentry_transaction,
    _refine_sentry_transaction_as_middleware,
)
from fastapi_turbo.requests import Request as _Request
from fastapi_turbo.responses import JSONResponse as _JSONResponse

_log = logging.getLogger("fastapi_turbo.applications")


def _run_pending_teardowns(*args, **kwargs):
    """Lazy bridge to ``applications._run_pending_teardowns``.

    Kept as a function rather than a top-level import to avoid a
    circular import between ``applications`` (owner of the teardown
    runner) and this module (which applications imports for the
    middleware wrap).
    """
    from fastapi_turbo.applications import _run_pending_teardowns as _impl
    return _impl(*args, **kwargs)


def _make_asgi_middleware_shim(mw_cls, kwargs):
    """Adapt a raw ASGI3 middleware class (``async __call__(scope, receive, send)``)
    into an ``@app.middleware("http")`` style callable ``async(request, call_next)``.

    The shim:
      * Builds a minimal ASGI scope from the ``Request``.
      * Constructs ``mw_cls(app=_inner_asgi_proxy, **kwargs)`` where the
        proxy, when called, invokes ``call_next(request)`` and relays the
        resulting response over the ASGI ``send`` channel.
      * Lets the middleware wrap ``receive`` (body-size guards,
        authentication, etc.) — ``HTTPException`` / other exceptions
        raised from the middleware propagate up and route through the
        app's ``exception_handlers`` as usual.
    """
    from fastapi_turbo.exceptions import HTTPException as _MW_HTTPExc
    from fastapi_turbo.responses import Response as _MW_Response

    async def _shim(request, call_next):
        # Collect body once; hand the cached copy to the middleware's
        # receive wrapper so it can observe the size or mutate bytes.
        body_bytes = await request.body() if hasattr(request, "body") else b""

        # Tracks whether the middleware invoked the inner app
        # (``_inner_asgi_proxy``). When it doesn't, the middleware
        # handled the request itself — Sentry's convention for
        # ``transaction_style="endpoint"`` is to treat the rejecting
        # middleware as the endpoint. We refine the transaction name
        # after ``instance(...)`` returns.
        inner_called = {"v": False}

        async def _receive():
            return {"type": "http.request", "body": body_bytes, "more_body": False}

        captured: dict = {}
        _sent_start = False

        async def _send(message):
            nonlocal _sent_start
            mtype = message.get("type")
            if mtype == "http.response.start":
                captured["status"] = message.get("status", 200)
                captured["headers"] = list(message.get("headers") or [])
                _sent_start = True
            elif mtype == "http.response.body":
                chunk = message.get("body", b"") or b""
                captured.setdefault("body", b"")
                captured["body"] += chunk

        async def _inner_asgi_proxy(scope, receive, send):
            # Pull the (possibly rewrapped) body out of ``receive`` so the
            # middleware can raise on over-size payloads before we invoke
            # the route handler.
            inner_called["v"] = True
            msg = await receive()
            # Pass the resulting response through to ``send`` so the
            # middleware sees it (headers etc. can be mutated by nesting
            # middlewares).
            try:
                resp = await call_next(request)
            except BaseException:
                # The handler raised a non-HTTPException. Starlette's
                # ``ServerErrorMiddleware`` would emit a 500 response via
                # ``send`` before the exception propagates, letting the
                # outer ASGI middlewares (e.g. ``SentryAsgiMiddleware``)
                # observe the status code in their wrapped ``send`` and
                # stamp ``transaction.status = internal_error``. Mirror
                # that pattern: emit the 500 frames, then re-raise.
                try:
                    await send({
                        "type": "http.response.start",
                        "status": 500,
                        "headers": [
                            (b"content-type", b"text/plain; charset=utf-8"),
                        ],
                    })
                    await send({
                        "type": "http.response.body",
                        "body": b"Internal Server Error",
                        "more_body": False,
                    })
                except Exception as _exc:  # noqa: BLE001
                    _log.debug("silent catch in applications: %r", _exc)
                raise
            # Materialise our Response into ASGI messages.
            status = getattr(resp, "status_code", 200)
            raw_headers = getattr(resp, "raw_headers", None) or [
                (k, v) for k, v in (getattr(resp, "headers", {}) or {}).items()
            ]
            header_list = []
            for k, v in raw_headers:
                if isinstance(k, str):
                    k = k.encode("latin-1")
                if isinstance(v, str):
                    v = v.encode("latin-1")
                header_list.append((k, v))
            body = getattr(resp, "body", b"") or b""
            if isinstance(body, str):
                body = body.encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": status,
                "headers": header_list,
            })
            await send({
                "type": "http.response.body",
                "body": body,
                "more_body": False,
            })
            return resp

        scope = dict(getattr(request, "scope", {}) or {})
        scope.setdefault("type", "http")
        try:
            instance = mw_cls(app=_inner_asgi_proxy, **kwargs)
        except TypeError:
            instance = mw_cls(**kwargs)
        try:
            await instance(scope, _receive, _send)
        except _MW_HTTPExc as exc:
            # Convert to a Response via the app's exception handlers if
            # one is registered; otherwise emit a generic JSON body.
            if _MW_HTTPExc in getattr(request, "app", None).exception_handlers:  # type: ignore[union-attr]
                handler = request.app.exception_handlers[_MW_HTTPExc]  # type: ignore[union-attr]
                result = handler(request, exc)
                if hasattr(result, "__await__"):
                    result = await result
                return result
            import json as _json
            detail = exc.detail
            if isinstance(detail, (dict, list)):
                body = _json.dumps({"detail": detail}).encode()
            else:
                body = _json.dumps({"detail": str(detail)}).encode()
            return _MW_Response(
                content=body,
                status_code=exc.status_code,
                media_type="application/json",
            )

        # Normal path: build a Response out of captured ASGI messages.
        if "status" in captured:
            # The middleware emitted a response without invoking the
            # inner proxy — Sentry expects the rejecting middleware
            # to be recorded as the transaction endpoint (for
            # ``transaction_style="endpoint"``). Doesn't apply to the
            # Sentry middleware itself.
            if not inner_called["v"]:
                try:
                    from sentry_sdk.integrations.asgi import (  # noqa: PLC0415
                        SentryAsgiMiddleware as _SentryASGI,
                    )
                except Exception as _exc:  # noqa: BLE001
                    _log.debug("silent catch in applications: %r", _exc)
                    _SentryASGI = None  # type: ignore[assignment]
                if _SentryASGI is None or mw_cls is not _SentryASGI:
                    try:
                        mw_name = f"{mw_cls.__module__}.{mw_cls.__qualname__}"
                    except AttributeError:
                        mw_name = getattr(mw_cls, "__name__", "middleware")
                    _refine_sentry_transaction_as_middleware(mw_cls, mw_name)
            resp = _MW_Response(
                content=captured.get("body", b""),
                status_code=captured["status"],
            )
            resp.headers.clear()
            resp.raw_headers.clear()
            for k, v in captured.get("headers", []):
                if isinstance(k, bytes):
                    k = k.decode("latin-1")
                if isinstance(v, bytes):
                    v = v.decode("latin-1")
                resp.headers.append(k, v)
            return resp
        # Middleware finished without emitting a response — fall through
        # to a direct ``call_next`` so the handler still runs.
        return await call_next(request)

    # Tag the shim with the original middleware class so reordering
    # (``_keep_sentry_outermost``) and introspection can identify it.
    _shim.__fastapi_turbo_mw_cls = mw_cls  # type: ignore[attr-defined]
    # Mark the shim so the in-process ASGI dispatcher can skip it
    # (the raw-ASGI chain already handles these MWs end-to-end — the
    # shim exists only for the Rust hot path, where requests arrive
    # as kwargs and need an ASGI scope synthesised).
    _shim._fastapi_turbo_is_asgi_shim = True  # type: ignore[attr-defined]
    return _shim


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
        # Seed the request-level body cache so middlewares can await
        # ``request.body()`` without having to walk back through Rust.
        # Custom ASGI middlewares (size limits, signing checks) need the
        # raw bytes BEFORE the handler runs.  The bytes kwarg is peeked
        # (not popped) so downstream handlers — RVE formatting, body
        # revalidation — still see it.
        _raw_body_bytes = kwargs.get("__fastapi_turbo_raw_body_bytes__")
        if _raw_body_bytes is None:
            _raw_body_str = kwargs.get("__fastapi_turbo_raw_body_str__")
            if isinstance(_raw_body_str, (bytes, bytearray)):
                _raw_body_bytes = bytes(_raw_body_str)
            elif isinstance(_raw_body_str, str):
                _raw_body_bytes = _raw_body_str.encode("utf-8")
        # Populate ``scheme`` + ``server`` so ASGI middleware that
        # builds URLs (``SentryAsgiMiddleware._get_url``) can produce
        # ``http://host:port/path`` instead of just ``/path``. Host
        # comes from the wire ``Host`` header when present.
        _hdrs = kwargs.pop("_request_headers", []) or []
        _host_h = ""
        for _k, _v in _hdrs:
            try:
                if (
                    _k.decode("latin-1") if isinstance(_k, bytes) else _k
                ).lower() == "host":
                    _host_h = (
                        _v.decode("latin-1") if isinstance(_v, bytes) else _v
                    )
                    break
            except UnicodeDecodeError:
                continue
        _server_host = _host_h
        _server_port = 80
        if ":" in _host_h:
            try:
                _server_host, _p = _host_h.rsplit(":", 1)
                _server_port = int(_p)
            except (ValueError, TypeError):
                _server_port = 80
        if not _server_host:
            _server_host = "testserver"
        return {
            "type": "http",
            "app": app,
            "method": kwargs.pop("_request_method", "GET"),
            "path": kwargs.pop("_request_path", "/"),
            "query_string": kwargs.pop("_request_query", "").encode(),
            "headers": _hdrs,
            "scheme": "http",
            "server": (_server_host, _server_port),
            "root_path": getattr(app, "root_path", "") or "",
            "_handler_kwargs": kwargs,
            "_body": _raw_body_bytes or b"",
        }

    def _call_handler_sync(kwargs):
        """Run the underlying handler, returning a Response-normalized value."""
        # Sentry transaction refinement — runs INSIDE any active
        # ``SentryAsgiMiddleware`` ``with start_transaction(...)`` block,
        # so set_transaction_name actually modifies the captured span.
        # The earlier Rust-bridge call only wins for exception_handler
        # request scope; transaction naming needs to happen here.
        try:
            _scope_now = _current_request_scope.get()
            if _scope_now is not None:
                _ep_now = _scope_now.get("endpoint")
                _rt_now = _scope_now.get("route")
                _rp_now = getattr(_rt_now, "path", None) if _rt_now is not None else None
                if _ep_now is not None or _rp_now is not None:
                    _refine_sentry_transaction(_ep_now, _rp_now)
            # Attach a Sentry event processor that fills
            # ``event["request"]["data"]`` (body) + ``cookies``. Stock
            # Sentry does this in its patched get_request_handler; that
            # patch is inert for us, so add the processor here where
            # we're inside Sentry's isolation scope.
            _maybe_install_sentry_request_event_processor(kwargs)
        except Exception as _exc:  # noqa: BLE001
            _log.debug("silent catch in applications: %r", _exc)
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
            endpoint, "_fastapi_turbo_defers_extraction_errors", False
        ):
            # Only strip the internal-only keys — every other kwarg is
            # a real handler arg resolved by Rust.
            _PRIVATE = {
                "_middleware_request",
                "__fastapi_turbo_extraction_errors__",
                "__fastapi_turbo_raw_body_str__",
                "__fastapi_turbo_raw_body_bytes__",
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
            from fastapi_turbo.exceptions import HTTPException as _HTTPExc
            # Sentry's ``failed_request_status_codes`` integration option
            # expects events for HTTPExceptions whose status is in the
            # configured set; stock Starlette fires these via
            # ExceptionMiddleware. We convert the exception right here,
            # so we have to trigger the capture ourselves.
            _maybe_sentry_capture_failed_request(exc)
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
                    # Capture so TestClient(raise_server_exceptions=True)
                    # re-raises in the caller, matching Starlette's ASGI
                    # test-client semantics.
                    try:
                        app._captured_server_exceptions.append(exc)
                    except Exception as _exc:  # noqa: BLE001
                        _log.debug("silent catch in applications: %r", _exc)
                    raise
                result = handled
            else:
                # No custom handler — capture for TestClient re-raise.
                if app is not None:
                    try:
                        app._captured_server_exceptions.append(exc)
                    except Exception as _exc:  # noqa: BLE001
                        _log.debug("silent catch in applications: %r", _exc)
                raise
        # Normalize raw handler return values into a ``Response`` before
        # the middleware chain sees them — FA's ExceptionMiddleware does
        # the same, and user middlewares that do
        # ``response.headers[...]`` assume a real Response. Default is
        # ``JSONResponse`` (matching FA's app-level default) so bare
        # strings get JSON-encoded (``"hello"`` not ``hello``).
        if hasattr(result, "status_code"):
            return result
        # None is a valid handler return value — FA encodes it as
        # JSONResponse(content=None) → body ``null``, status 200.
        # Middlewares that do ``response.headers[...]`` assume a Response.
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
                from fastapi_turbo.background import BackgroundTasks as _BGT
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
    # Preserve the original user endpoint reference through the
    # middleware wrapper so Sentry endpoint-style transaction naming
    # (which calls ``transaction_from_function(endpoint)``) sees
    # ``tests.foo._message`` instead of our wrapper's qualified name.
    try:
        wrapped_sync._fastapi_turbo_original_endpoint = getattr(  # type: ignore[attr-defined]
            endpoint, "_fastapi_turbo_original_endpoint", endpoint,
        )
    except (AttributeError, TypeError):
        pass
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



