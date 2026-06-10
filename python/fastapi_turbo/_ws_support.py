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
"""

from __future__ import annotations

import inspect
import typing
from typing import Any, Callable


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

        instance = _endpoint_cls()
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
