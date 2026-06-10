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
# fastapi`` pre-shim at its line 18) — always BEFORE the compat shim
# shadows ``sys.modules["fastapi"]`` — so this resolves to the real
# package. ``APIRoute`` below is a THIN SUBCLASS of the real one.
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


def _looks_like_starlette_mount(route: Any) -> bool:
    cls_name = type(route).__name__
    return cls_name == "Mount" or (
        hasattr(route, "routes")
        and hasattr(route, "app")
        and not hasattr(route, "endpoint")
    )


def _looks_like_starlette_websocket_route(route: Any) -> bool:
    return (
        getattr(route, "_is_websocket", False)
        or type(route).__name__ == "WebSocketRoute"
    )


def _mark_starlette_compat_route(route: Any) -> None:
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


class APIRouter:
    """Route collection that mirrors FastAPI's APIRouter."""

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
        self._assert_no_pydantic_v1_models(endpoint, kwargs)
        self._assert_path_params_are_scalars(path, endpoint)
        self._assert_query_params_are_supported(endpoint)
        self._assert_response_models_are_valid(kwargs)
        self._maybe_require_multipart(endpoint)
        self._assert_dep_scopes(endpoint)
        self._assert_param_annotations(path, endpoint)
        # Honour ``APIRouter(route_class=...)`` — users subclass APIRoute
        # to attach custom attrs / override request handling.
        route_cls = self.route_class or APIRoute
        route = route_cls(path, endpoint, methods=methods, **kwargs)
        self.routes.append(route)

    @staticmethod
    def _assert_dep_scopes(endpoint: Callable) -> None:
        """FA 0.120+: a ``Depends(..., scope="request")`` dep cannot
        depend on ``Depends(..., scope="function")`` sub-deps. Raised at
        decoration time via ``FastAPIError``.
        """
        import inspect as _inspect
        import typing as _typing
        from fastapi_turbo.dependencies import Depends as _Depends
        from fastapi_turbo.exceptions import FastAPIError as _FE

        def _marker_scope(marker) -> str:
            s = getattr(marker, "scope", None)
            return s if s in ("function", "request") else "request"

        def _collect_depends(fn):
            """Yield (param_name, Depends marker) for every Depends on fn's sig."""
            try:
                sig = _safe_signature(fn)
            except (TypeError, ValueError):
                return
            except NameError:
                return
            for pname, param in sig.parameters.items():
                default = param.default
                if isinstance(default, _Depends):
                    yield pname, default
                    continue
                ann = param.annotation
                if _typing.get_origin(ann) is _typing.Annotated:
                    for meta in _typing.get_args(ann)[1:]:
                        if isinstance(meta, _Depends):
                            yield pname, meta
                            break

        seen: set[int] = set()

        def _walk(callable_):
            if callable_ is None or id(callable_) in seen:
                return
            seen.add(id(callable_))
            for _pn, sub_marker in _collect_depends(callable_):
                sub_scope = _marker_scope(sub_marker)
                sub_callable = sub_marker.dependency
                # Walk deeper so we can match FA's specific "outer request
                # cannot depend on function" rule at any depth.
                _walk(sub_callable)

        def _is_yield_dep(fn) -> bool:
            """True if ``fn`` is a generator/async-generator dep (teardown-carrying)."""
            if fn is None:
                return False
            import inspect as _i
            if _i.isgeneratorfunction(fn) or _i.isasyncgenfunction(fn):
                return True
            # Class instances whose __call__ is a generator.
            call = getattr(fn, "__call__", None)
            if call is not None and not isinstance(fn, type):
                if _i.isgeneratorfunction(call) or _i.isasyncgenfunction(call):
                    return True
            return False

        # The scope rule only applies to yield (generator) deps — non-yield
        # deps have no teardown, so scope is irrelevant. FA enforces this
        # at decoration time by raising ``FastAPIError`` when a request-
        # scope yield dep depends on a function-scope yield dep.
        for _pn, top_marker in _collect_depends(endpoint):
            outer_scope = _marker_scope(top_marker)
            outer_callable = top_marker.dependency
            if outer_callable is None or not _is_yield_dep(outer_callable):
                continue
            for _sub_pn, sub_marker in _collect_depends(outer_callable):
                sub_scope = _marker_scope(sub_marker)
                sub_callable = sub_marker.dependency
                if outer_scope == "request" and sub_scope == "function" and _is_yield_dep(sub_callable):
                    _outer_name = getattr(outer_callable, "__name__", repr(outer_callable))
                    raise _FE(
                        f'The dependency "{_outer_name}" has a scope of "request", '
                        f'it cannot depend on dependencies with scope "function"'
                    )
            _walk(outer_callable)

    @staticmethod
    def _assert_param_annotations(path: str, endpoint: Callable) -> None:
        """FA parity: raise ``AssertionError`` at decoration time for
        parameter annotation patterns FA rejects:

        - ``Annotated[T, Path(default=...)]`` on a path param
        - ``Annotated[T, Query(default=...)]`` (default must be ``=``)
        - ``Annotated[T, Depends(x)] = Depends(x)`` (doubled marker)
        - ``Annotated[T, Query(...)] = Depends(x)`` (mixed markers)
        """
        import inspect as _inspect
        import typing as _typing
        from fastapi_turbo.param_functions import _ParamMarker as _PM
        from fastapi_turbo.dependencies import Depends as _Dep

        import re as _re
        path_param_names = set(_re.findall(r"\{([^}:]+)", path))
        try:
            sig = _safe_signature(endpoint)
        except (TypeError, ValueError):
            return
        from pydantic_core import PydanticUndefined as _Und
        for pname, p in sig.parameters.items():
            ann = p.annotation
            default = p.default
            if _typing.get_origin(ann) is not _typing.Annotated:
                continue
            metas = _typing.get_args(ann)[1:]
            markers_in_ann = [m for m in metas if isinstance(m, _PM)]
            depends_in_ann = [m for m in metas if isinstance(m, _Dep)]
            # Path(default=...) or Query(default=...) in Annotated
            for m in markers_in_ann:
                _d = getattr(m, "default", _Und)
                if _d is _Und or _d is Ellipsis:
                    continue
                kind = getattr(m, "_kind", "")
                if kind == "path":
                    assert False, (
                        "Path parameters cannot have a default value"
                    )
                if kind in ("query", "header", "cookie"):
                    assert False, (
                        f"`{kind.capitalize()}` default value cannot be set "
                        f"in `Annotated` for {pname!r}. Set the default "
                        f"value with `=` instead."
                    )
            # Depends in Annotated + also Depends as default value
            if depends_in_ann and isinstance(default, _Dep):
                assert False, (
                    f"Cannot specify `Depends` in `Annotated` and default "
                    f"value together for {pname!r}"
                )
            # Query/Path/... in Annotated + Depends as default value
            if markers_in_ann and isinstance(default, _Dep):
                assert False, (
                    f"Cannot specify a FastAPI annotation in `Annotated` "
                    f"and `Depends` as a default value together for "
                    f"{pname!r}"
                )

    @staticmethod
    def _maybe_require_multipart(endpoint: Callable) -> None:
        """FA raises ``RuntimeError`` at decoration time if the handler
        uses ``Form()`` / ``File()`` without ``python-multipart``
        installed. We mirror that check so
        ``test_multipart_installation`` and other suites that rely on
        the install-guard behaviour pass. When multipart IS available,
        this is a no-op.
        """
        import inspect as _inspect
        import typing as _typing
        try:
            sig = _safe_signature(endpoint)
        except (TypeError, ValueError):
            return
        from fastapi_turbo.param_functions import Form as _Form, File as _File
        uses_multipart = False
        for p in sig.parameters.values():
            if isinstance(p.default, (_Form, _File)):
                uses_multipart = True
                break
            ann = p.annotation
            if _typing.get_origin(ann) is _typing.Annotated:
                for meta in _typing.get_args(ann)[1:]:
                    if isinstance(meta, (_Form, _File)):
                        uses_multipart = True
                        break
            if uses_multipart:
                break
        if not uses_multipart:
            return
        try:
            from fastapi.dependencies.utils import (  # type: ignore[import-not-found]
                ensure_multipart_is_installed as _ensure,
            )
            _ensure()
        except RuntimeError:
            raise
        except Exception:  # noqa: BLE001
            # Shim unavailable — don't block registration.
            pass

    @staticmethod
    def _assert_path_params_are_scalars(path: str, endpoint: Callable) -> None:
        """FA raises ``AssertionError`` at decoration time when a path
        parameter is typed as a non-scalar (``list[Item]``,
        ``tuple[X,Y]``, ``dict[...]``, ``set[...]`` — anything iterable
        that can't be encoded in a URL segment). Match that surface.
        """
        import inspect as _inspect
        import re as _re
        import typing as _typing
        try:
            names = set(_re.findall(r"\{([^}:/]+)", path))
        except Exception:  # noqa: BLE001
            return
        if not names:
            return
        try:
            sig = _safe_signature(endpoint)
        except (TypeError, ValueError):
            return
        try:
            hints = _typing.get_type_hints(endpoint, include_extras=True)
        except Exception:  # noqa: BLE001
            hints = {}
        bad_origins = (list, tuple, set, frozenset, dict)
        for pname in names:
            if pname not in sig.parameters:
                continue
            ann = hints.get(pname, sig.parameters[pname].annotation)
            if ann is _inspect.Parameter.empty:
                continue
            if _typing.get_origin(ann) is _typing.Annotated:
                inner = _typing.get_args(ann)
                if inner:
                    ann = inner[0]
            origin = _typing.get_origin(ann)
            if origin in bad_origins or ann in bad_origins:
                raise AssertionError(
                    f"Path parameter {pname!r} has invalid type {ann!r}: "
                    f"non-scalar container types cannot be used in path "
                    f"parameters (FA/Starlette limitation)."
                )

    @staticmethod
    @staticmethod
    def _assert_no_pydantic_v1_models(endpoint: Callable, kwargs: dict) -> None:
        """FA 0.120+: raise ``PydanticV1NotSupportedError`` at decoration
        time when any param annotation, return annotation, response_model,
        responses[code].model, or Union arm references a
        ``pydantic.v1.BaseModel`` subclass. fastapi-turbo requires v2.
        """
        try:
            from pydantic import v1 as _pd_v1
        except ImportError:
            return
        from fastapi_turbo.exceptions import PydanticV1NotSupportedError as _V1Err
        import typing as _typing

        def _is_v1(t) -> bool:
            if isinstance(t, type):
                try:
                    if issubclass(t, _pd_v1.BaseModel):
                        return True
                except TypeError:
                    return False
            return False

        def _walk(t) -> None:
            if t is None or t is _typing.Any:
                return
            if _is_v1(t):
                raise _V1Err(
                    "Pydantic v1 models are not supported. Migrate to Pydantic v2."
                )
            for sub in _typing.get_args(t):
                _walk(sub)

        # Endpoint signature — params + return annotation.
        try:
            sig = _safe_signature(endpoint)
        except (TypeError, ValueError):
            return
        for p in sig.parameters.values():
            if p.annotation is not p.empty:
                _walk(p.annotation)
        if sig.return_annotation is not sig.empty:
            _walk(sig.return_annotation)
        # Explicit response_model and responses[code].model kwargs.
        if "response_model" in kwargs:
            _walk(kwargs.get("response_model"))
        for _code, _spec in (kwargs.get("responses") or {}).items():
            if isinstance(_spec, dict):
                _walk(_spec.get("model"))

    @staticmethod
    def _assert_response_models_are_valid(kwargs: dict) -> None:
        """FA raises ``FastAPIError`` at decoration time when a
        ``response_model=`` or a ``responses={code: {"model": ...}}``
        references a non-Pydantic type. Mirror that behaviour.
        """
        from fastapi_turbo.exceptions import FastAPIError as _FAErr
        import typing as _typing

        def _valid_response_type(t) -> bool:
            if t is None:
                return True
            try:
                from pydantic import BaseModel as _BM, TypeAdapter as _TA
                if isinstance(t, type) and issubclass(t, _BM):
                    return True
            except Exception:  # noqa: BLE001
                return False
            # Streaming return annotations (FA special-cases these for
            # SSE / JSONL generators). Accepted without further checks.
            try:
                import collections.abc as _cabc
                _stream_origins = {
                    _cabc.AsyncIterable, _cabc.AsyncIterator,
                    _cabc.AsyncGenerator, _cabc.Iterable,
                    _cabc.Iterator, _cabc.Generator,
                }
                if _typing.get_origin(t) in _stream_origins:
                    return True
            except Exception:  # noqa: BLE001
                pass
            # Walk generic containers — list[T] / tuple[T, ...] / dict[K,V].
            origin = _typing.get_origin(t)
            if origin in (list, set, frozenset, tuple):
                for sub in _typing.get_args(t):
                    if sub is type(None) or sub is Ellipsis:
                        continue
                    if not _valid_response_type(sub):
                        return False
                return True
            if origin is dict:
                vs = _typing.get_args(t)
                if len(vs) == 2 and not _valid_response_type(vs[1]):
                    return False
                return True
            if origin is _typing.Union:
                for sub in _typing.get_args(t):
                    if sub is type(None):
                        continue
                    if not _valid_response_type(sub):
                        return False
                return True
            # Primitives / Any / forward refs are fine.
            if isinstance(t, type) and t in (int, float, str, bool, bytes, list, dict, tuple, set, frozenset, type(None)):
                return True
            # TypeAdapter round-trip — if it succeeds, Pydantic handles it.
            try:
                _TA(t)
                return True
            except Exception:  # noqa: BLE001
                return False

        rm = kwargs.get("response_model")
        if rm is not None and not _valid_response_type(rm):
            raise _FAErr(
                f"Invalid args for response field! Hint: check that "
                f"{rm!r} is a valid Pydantic field type."
            )
        for code, spec in (kwargs.get("responses") or {}).items():
            if isinstance(spec, dict) and spec.get("model") is not None:
                m = spec["model"]
                if not _valid_response_type(m):
                    raise _FAErr(
                        f"Invalid args for response field! Hint: check "
                        f"that {m!r} is a valid Pydantic field type."
                    )

    @staticmethod
    def _assert_query_params_are_supported(endpoint: Callable) -> None:
        """FA raises ``AssertionError`` at decoration time for Query
        params typed as container of BaseModels (``list[Item]``,
        ``tuple[Item,Item]``, ``dict[str, Item]``) or bare ``dict``.
        Only scalar sequences (``list[str]``, ``list[int]``, ...) are
        allowed. Mirror that behaviour.
        """
        import inspect as _inspect
        import typing as _typing
        from fastapi_turbo.param_functions import Query as _Query

        try:
            sig = _safe_signature(endpoint)
        except (TypeError, ValueError):
            return
        try:
            hints = _typing.get_type_hints(endpoint, include_extras=True)
        except Exception:  # noqa: BLE001
            hints = {}

        def _is_query_param(param, ann) -> bool:
            default = param.default
            if isinstance(default, _Query):
                return True
            if _typing.get_origin(ann) is _typing.Annotated:
                for meta in _typing.get_args(ann)[1:]:
                    if isinstance(meta, _Query):
                        return True
            return False

        def _container_of_model(ann) -> bool:
            from pydantic import BaseModel as _BM
            if _typing.get_origin(ann) is _typing.Annotated:
                ann = _typing.get_args(ann)[0]
            origin = _typing.get_origin(ann)
            if origin in (dict,) or ann is dict:
                return True  # dict[str, X] / bare dict not allowed as Query
            if origin in (list, tuple, set, frozenset):
                for sub in _typing.get_args(ann):
                    if (
                        isinstance(sub, type)
                        and issubclass(sub, _BM)
                    ):
                        return True
            # bare ``dict | None`` — check union of dicts
            if _is_union_origin(origin):
                for sub in _typing.get_args(ann):
                    if sub is dict or _typing.get_origin(sub) is dict:
                        return True
            return False

        for pname, param in sig.parameters.items():
            ann = hints.get(pname, param.annotation)
            if ann is _inspect.Parameter.empty:
                continue
            if not _is_query_param(param, ann):
                continue
            if _container_of_model(ann):
                raise AssertionError(
                    f"Query parameter {pname!r} must be one of the supported types"
                )

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

        # Resolve the response_class cascade ONCE per included route
        # so route collection doesn't have to walk the
        # router/include tree at request time. Cascade matches
        # upstream: route own → child include_router default →
        # child router default → … → outermost include
        # default_response_class → outermost router default. The
        # walker threads ``parent_default`` through recursion so a
        # nested router with no default still inherits the
        # outermost ancestor's default.
        outer_default = (
            default_response_class
            if default_response_class is not None
            else getattr(router, "default_response_class", None)
        )

        def _mirror(
            src_router, pfx: str, parent_default, parent_deps
        ) -> None:
            own_default = getattr(src_router, "default_response_class", None)
            eff_default = own_default if own_default is not None else parent_default
            # ``parent_deps`` is the dep chain accumulated from the
            # outermost include down to (but not including) this
            # router's own deps. Append this router's own deps so
            # routes registered directly on it get the full chain.
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
                    and _unset_to_none(getattr(clone, "response_class", None)) is None
                    and getattr(clone, "_fastapi_turbo_effective_response_class", None)
                    is None
                ):
                    clone._fastapi_turbo_effective_response_class = eff_default
                if eff_extra_deps:
                    clone._fastapi_turbo_include_deps = list(eff_extra_deps)
                self.routes.append(clone)
            for entry in getattr(src_router, "_included_routers", []):
                child_router, child_prefix = entry[0], entry[1]
                # Per-include kwarg (entry[3]['default_response_class'])
                # also overrides for that subtree only.
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
                child_include_deps = (
                    list(child_meta.get("dependencies", []) or [])
                    if isinstance(child_meta, dict)
                    else []
                )
                nested_parent_deps = (
                    list(eff_extra_deps) + child_include_deps
                )
                nested_prefix = _stack_path(
                    _stack_path(pfx, child_prefix or ""),
                    getattr(child_router, "prefix", "") or "",
                )
                _mirror(
                    child_router,
                    nested_prefix,
                    nested_default,
                    nested_parent_deps,
                )

        _mirror(router, full_prefix, outer_default, list(dependencies or []))
