"""WebSocket route support (door-scope).

WebSocket routes never construct a real ``fastapi.routing.APIRoute`` —
turbo's ``WebSocket`` is a standalone class (not real starlette's), so
real FastAPI's introspection would reject the endpoint at decoration
time. Everything WS-specific therefore lives here:

- ``WSRoute`` — the lightweight metadata holder registered on routers
  for ``@app.websocket(...)`` endpoints (the attribute surface the
  route-collection WS branch reads: path/endpoint/name/tags/
  dependencies/_is_websocket).
- ``_adapt_websocket_endpoint_class`` — Starlette ``WebSocketEndpoint``
  class → FastAPI handler-shape adapter.
- ``_ws_check_scope_mismatch`` — FA 0.120+ decoration-time scope-rule
  check (no real route is built on the WS path, so real FastAPI never
  gets a chance to raise it natively).
- ``_wrap_websocket_endpoint`` — the full door-side WS request pipeline
  (deps incl. sub-deps + yield, scalar validation, WS exception →
  reject/close mapping, TestClient exception capture).
- ``_ws_entry_with_asgi_chain`` — dispatches a synthesised WS scope
  through the app's raw ASGI middleware chain.
"""

from __future__ import annotations

import inspect
import logging
import typing
from typing import Any, Callable

_log = logging.getLogger("fastapi_turbo._ws_support")


def _safe_signature(endpoint):
    """Like ``inspect.signature(endpoint)``, but on Python 3.14+ falls
    back to ``FORWARDREF`` annotation format so forward-referenced
    types (names only defined under ``if TYPE_CHECKING:``) don't blow
    up at decorator time with a ``NameError``. We only need to walk
    parameter names / markers here, never evaluate the annotations.
    """
    try:
        try:
            import annotationlib as _al  # py3.14+
            return inspect.signature(
                endpoint, annotation_format=_al.Format.FORWARDREF
            )
        except ImportError:
            return inspect.signature(endpoint)
    except NameError:
        # Belt-and-braces: older 3.14 pre-releases or 3.13 PEP 649
        # back-port may still eagerly eval. In that case we just
        # skip the check — the resolver handles the annotation
        # later with ``get_type_hints`` and its own fallbacks.
        raise
    except (TypeError, ValueError):
        raise


def _is_websocket_endpoint_class(endpoint: Any) -> bool:
    if not isinstance(endpoint, type):
        return False
    return any(base.__name__ == "WebSocketEndpoint" for base in endpoint.__mro__)


async def _maybe_await_ws_result(result: Any) -> Any:
    if inspect.isawaitable(result):
        return await result
    return result


def _adapt_websocket_endpoint_class(endpoint: Callable) -> Callable:
    """Adapt Starlette ``WebSocketEndpoint`` classes to FastAPI handler shape."""
    if not _is_websocket_endpoint_class(endpoint):
        return endpoint

    async def _endpoint(websocket, _endpoint_cls=endpoint):
        from fastapi_turbo.exceptions import WebSocketDisconnect as _WSD

        try:
            instance = _endpoint_cls()
        except TypeError:
            # REAL Starlette ``WebSocketEndpoint.__init__`` requires
            # ``(scope, receive, send)`` (it asserts ``scope["type"] ==
            # "websocket"``). The adapter below drives ``on_connect`` /
            # ``on_receive`` / ``on_disconnect`` directly through the turbo
            # WebSocket wrapper, so the stored receive/send are never used —
            # hand it the wrapper's scope and inert callables.
            _scope = getattr(websocket, "scope", None)
            if not isinstance(_scope, dict) or _scope.get("type") != "websocket":
                _scope = {"type": "websocket"}

            async def _inert_receive():
                return {"type": "websocket.disconnect", "code": 1000}

            async def _inert_send(message):
                return None

            instance = _endpoint_cls(_scope, _inert_receive, _inert_send)
        close_code = 1000
        try:
            await _maybe_await_ws_result(instance.on_connect(websocket))
            while True:
                encoding = getattr(instance, "encoding", None)
                if encoding == "text":
                    message = await websocket.receive_text()
                elif encoding == "bytes":
                    message = await websocket.receive_bytes()
                elif encoding == "json":
                    message = await websocket.receive_json()
                else:
                    message = await websocket.receive()
                    if message.get("type") == "websocket.disconnect":
                        close_code = message.get("code", 1000)
                        break
                    if "text" in message:
                        message = message["text"]
                    elif "bytes" in message:
                        message = message["bytes"]
                await _maybe_await_ws_result(
                    instance.on_receive(websocket, message)
                )
        except _WSD as exc:
            close_code = getattr(exc, "code", 1000)
        except Exception:
            close_code = 1011
            raise
        finally:
            await _maybe_await_ws_result(
                instance.on_disconnect(websocket, close_code)
            )

    _endpoint.__name__ = getattr(endpoint, "__name__", "websocket_endpoint")
    return _endpoint


def _ws_check_scope_mismatch(endpoint: Callable) -> None:
    """Raise ``FastAPIError`` at WS-route decoration time when a
    request-scope yield dep depends on a function-scope yield dep.

    FastAPI 0.120+ rule — our runtime resolution already honours it,
    but the test suite asserts ``pytest.raises(FastAPIError)`` fires on
    the decorator itself, so we replicate the check synchronously.
    """
    from fastapi_turbo.dependencies import Depends as _Depends
    from fastapi_turbo.exceptions import FastAPIError as _FE

    def _get_scope(dep) -> str:
        s = getattr(dep, "scope", None)
        return s if s in ("function", "request") else "request"

    def _extract_dep(annotation, default):
        if isinstance(default, _Depends):
            return default
        if typing.get_origin(annotation) is typing.Annotated:
            for m in typing.get_args(annotation)[1:]:
                if isinstance(m, _Depends):
                    return m
        return None

    def _walk(dep, visited: set) -> None:
        dep_func = dep.dependency
        if dep_func is None or id(dep_func) in visited:
            return
        visited.add(id(dep_func))
        try:
            sig = _safe_signature(dep_func)
        except (TypeError, ValueError, NameError):
            return
        try:
            hints = typing.get_type_hints(dep_func, include_extras=True)
        except Exception:  # noqa: BLE001
            hints = {}
        outer_scope = _get_scope(dep)
        outer_yield = (
            inspect.isgeneratorfunction(dep_func)
            or inspect.isasyncgenfunction(dep_func)
        )
        for p_name, p in sig.parameters.items():
            ann = hints.get(p_name, p.annotation)
            sub = _extract_dep(ann, p.default)
            if sub is None or sub.dependency is None:
                continue
            sub_scope = _get_scope(sub)
            sub_yield = (
                inspect.isgeneratorfunction(sub.dependency)
                or inspect.isasyncgenfunction(sub.dependency)
            )
            if (
                outer_yield and sub_yield
                and outer_scope == "request" and sub_scope == "function"
            ):
                outer_name = getattr(dep_func, "__name__", repr(dep_func))
                raise _FE(
                    f'The dependency "{outer_name}" has a scope of "request", '
                    f'it cannot depend on dependencies with scope "function"'
                )
            _walk(sub, visited)

    try:
        sig = inspect.signature(endpoint)
    except (TypeError, ValueError):
        return
    try:
        hints = typing.get_type_hints(endpoint, include_extras=True)
    except Exception:  # noqa: BLE001
        hints = {}
    for p_name, p in sig.parameters.items():
        ann = hints.get(p_name, p.annotation)
        dep = _extract_dep(ann, p.default)
        if dep is None or dep.dependency is None:
            continue
        _walk(dep, set())


class WSRoute:
    """Lightweight WebSocket route metadata holder.

    Replaces the clone-``APIRoute(methods=["GET"], _is_websocket=True)``
    registration: WS endpoints are typed against turbo's standalone
    ``WebSocket`` class, which a real ``fastapi.routing.APIRoute``
    rejects at construction, so WS routes carry a plain holder instead.

    The attribute surface is exactly what the route-collection WS
    branch reads: ``path``, ``endpoint``, ``name``, ``tags``,
    ``dependencies``, ``_is_websocket``. Unknown kwargs are swallowed
    (the clone APIRoute did the same via ``**kwargs``).
    """

    _is_websocket = True

    def __init__(
        self,
        path: str,
        endpoint: Callable,
        *,
        name: str | None = None,
        dependencies: Any = None,
        tags: list | None = None,
        **kwargs: Any,
    ):
        self.path = path
        self.endpoint = endpoint
        if name:
            self.name = name
        else:
            # Endpoints can be ``functools.partial`` wrappers or callable
            # class instances without ``__name__`` — same fallback chain
            # as the HTTP route's clone-style ``get_name``.
            ep = endpoint
            inner = getattr(ep, "func", None)
            if inner is not None:
                ep = inner
            self.name = (
                getattr(ep, "__name__", None)
                or type(endpoint).__name__
            )
        self.dependencies = list(dependencies or [])
        self.tags = list(tags or [])


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


def _wrap_websocket_endpoint(
    app,
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
                _DoorWebSocketRequestValidationError as _WRVE,
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

    # Identify the WebSocket parameter. Prefer a ``WebSocket``-annotated
    # param; otherwise fall back to the FIRST positional param (FastAPI
    # tutorial style ``async def ws(websocket, ...)`` where the connection
    # arg is often untyped). Mirrors the in-process dispatcher the door
    # replaced — without this, an untyped ``websocket`` would be
    # misclassified as a required Query param and close the socket 1008.
    _ws_fallback_name = None
    if sig is not None:
        _has_annotated_ws = any(
            _is_websocket_annotation(n, p.annotation)
            for n, p in sig.parameters.items()
        )
        if not _has_annotated_ws:
            for n, p in sig.parameters.items():
                if p.kind not in (
                    _inspect.Parameter.POSITIONAL_ONLY,
                    _inspect.Parameter.POSITIONAL_OR_KEYWORD,
                ):
                    continue
                _r_ann = type_hints.get(n, p.annotation)
                if _extract_depends(_r_ann, p.default) is not None:
                    continue
                if _extract_marker(_r_ann, p.default)[0] is not None:
                    continue
                _ws_fallback_name = n
                break

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

            if _is_websocket_annotation(name, raw_ann) or name == _ws_fallback_name:
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
    app_ref = app

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
        if getattr(ws, "_ws", None) is None:
            # In-process ASGI door: emit the WS close with the REAL code
            # regardless of accept state (a pre-accept close IS the
            # handshake rejection here, and the TestClient reads this
            # frame's code — so it must be 1008 etc., not the HTTP 403).
            ws._asgi_queue_close(code, reason)
            return
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
    # the path template. ``WSRoute`` IS the class the shim binds as
    # ``fastapi.routing.APIWebSocketRoute``, so isinstance checks
    # see one consistent type.
    from fastapi_turbo._ws_support import WSRoute as _APIWSRoute
    _synthetic_route = _APIWSRoute(
        route_path,
        endpoint,
        name=getattr(endpoint, "__name__", "") or None,
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
                if getattr(ws, "_ws", None) is None:
                    ws._asgi_queue_close(1008, "validation error")
                elif ws.application_state == _WSState.CONNECTING:
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
                if getattr(ws, "_ws", None) is None:
                    # In-process ASGI door: close with 1011 (internal
                    # error) regardless of accept state — matches the
                    # generic-error close code the dispatcher emitted.
                    ws._asgi_queue_close(1011, "")
                elif ws.application_state == _WSState.CONNECTING:
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
    app_self = app

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
