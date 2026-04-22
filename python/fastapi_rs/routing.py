"""Routing primitives matching FastAPI's interface."""

from __future__ import annotations

import inspect
import re
import typing
from typing import Any, Callable, Sequence
from urllib.parse import quote


def _default_generate_unique_id(route: "APIRoute", method: str) -> str:
    """Default function to generate a unique operation ID for OpenAPI."""
    return f"{route.name}_{method.lower()}"


def _ws_check_scope_mismatch(endpoint: Callable) -> None:
    """Raise ``FastAPIError`` at WS-route decoration time when a
    request-scope yield dep depends on a function-scope yield dep.

    FastAPI 0.120+ rule — our runtime resolution already honours it,
    but the test suite asserts ``pytest.raises(FastAPIError)`` fires on
    the decorator itself, so we replicate the check synchronously.
    """
    from fastapi_rs.dependencies import Depends as _Depends
    from fastapi_rs.exceptions import FastAPIError as _FE

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
            sig = inspect.signature(dep_func)
        except (TypeError, ValueError):
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


_UNSET = object()
"""Sentinel for distinguishing ``response_model=None`` (explicit — skip
model validation) from ``response_model`` being omitted (auto-derive from
the return annotation). FA does the same — ``default=Default(None)``."""


class APIRoute:
    """Metadata for a single registered route."""

    def __init__(
        self,
        path: str,
        endpoint: Callable,
        *,
        methods: list[str] | None = None,
        response_model: Any = _UNSET,
        response_model_include: set | None = None,
        response_model_exclude: set | None = None,
        response_model_exclude_unset: bool = False,
        response_model_exclude_defaults: bool = False,
        response_model_exclude_none: bool = False,
        response_model_by_alias: bool = True,
        status_code: int | None = None,
        tags: list[str] | None = None,
        summary: str | None = None,
        description: str | None = None,
        response_description: str = "Successful Response",
        responses: dict | None = None,
        name: str | None = None,
        deprecated: bool | None = None,
        operation_id: str | None = None,
        generate_unique_id_function: Callable | None = None,
        dependencies: Sequence | None = None,
        response_class: Any = None,
        include_in_schema: bool = True,
        openapi_extra: dict | None = None,
        security: list | None = None,
        callbacks: list | None = None,
        servers: list[dict[str, Any]] | None = None,
        external_docs: dict[str, Any] | None = None,
        **kwargs: Any,
    ):
        self.path = path
        self.endpoint = endpoint
        self.methods = [m.upper() for m in (methods or ["GET"])]
        # FastAPI auto-derives `response_model` from the handler's return
        # annotation when the caller didn't set one explicitly. Mirror that
        # so OpenAPI for endpoints like `def root() -> dict[str, str]` gets
        # the full `{type: object, additionalProperties: ...}` schema. Use
        # `typing.get_type_hints` so string annotations (`from __future__
        # import annotations`) are resolved to real types.
        if response_model is _UNSET:
            # User didn't pass ``response_model`` — auto-derive from the
            # return annotation.
            import inspect as _inspect
            import typing as _typing
            derived = None
            try:
                hints = _typing.get_type_hints(endpoint, include_extras=False)
                _ra = hints.get("return")
                if _ra is None:
                    _ra = _inspect.signature(endpoint).return_annotation
                    if _ra is _inspect.Signature.empty:
                        _ra = None
                derived = _ra
            except (TypeError, ValueError, NameError):
                pass
            # FA parity: ``-> Response`` (or subclass) is a RESPONSE-
            # CLASS hint, not a Pydantic response_model. Drop it so the
            # route bypasses response_model filtering. Streaming return
            # types (``AsyncIterable[T]`` etc.) are KEPT because the SSE
            # / JSONL OpenAPI emitters use them to register the item
            # model under components.schemas.
            try:
                from fastapi_rs.responses import Response as _RespCls
                if isinstance(derived, type) and issubclass(derived, _RespCls):
                    derived = None
            except ImportError:
                pass
            response_model = derived
            # FA parity: validate the DERIVED response_model too —
            # ``def f() -> Response | None`` (Union of Response + None)
            # should error at decoration because ``Response`` isn't a
            # valid Pydantic field type. Pure ``Response`` was already
            # dropped above.
            if response_model is not None:
                try:
                    APIRouter._assert_response_models_are_valid(
                        {"response_model": response_model},
                    )
                except Exception as _e:
                    from fastapi_rs.exceptions import FastAPIError as _FAErr
                    if isinstance(_e, _FAErr):
                        raise _FAErr(
                            str(_e) + " If you don't need to use the "
                            "response field, you can set the parameter "
                            "response_model=None to skip response model generation."
                        ) from None
                    raise
        elif response_model is None:
            # Explicit ``response_model=None`` — FA treats this as "skip
            # response-model filtering entirely, even if the handler has
            # a return annotation". Keep it as None.
            pass
        self.response_model = response_model
        self.response_model_include = response_model_include
        self.response_model_exclude = response_model_exclude
        self.response_model_exclude_unset = response_model_exclude_unset
        self.response_model_exclude_defaults = response_model_exclude_defaults
        self.response_model_exclude_none = response_model_exclude_none
        self.response_model_by_alias = response_model_by_alias
        self.status_code = status_code
        self.tags = tags or []
        self.summary = summary
        # FA: when description= isn't set, falls back to the endpoint's
        # docstring (``inspect.cleandoc(endpoint.__doc__)``). Matches
        # FA's ``get_openapi`` which also truncates at the first ``\f``
        # (formfeed) — text after ``\f`` is considered private
        # (``:param`` / internal notes).
        if description is None:
            _raw_doc = getattr(endpoint, "__doc__", None)
            if _raw_doc:
                import inspect as _ins
                description = _ins.cleandoc(_raw_doc)
        if isinstance(description, str) and "\f" in description:
            description = description.split("\f", 1)[0].rstrip("\n")
        self.description = description
        self.response_description = response_description
        self.responses = responses or {}
        # Endpoints can be ``functools.partial`` wrappers or callable
        # class instances that don't carry ``__name__``. Fall back to
        # the wrapped function's name, then the class name, then
        # "endpoint" — matches FastAPI's ``get_name`` helper.
        if name:
            self.name = name
        else:
            ep = endpoint
            inner = getattr(ep, "func", None)
            if inner is not None:
                ep = inner
            self.name = (
                getattr(ep, "__name__", None)
                or type(endpoint).__name__
            )
        self.deprecated = bool(deprecated) if deprecated is not None else False
        self.dependencies = list(dependencies or [])
        self.response_class = response_class
        self.include_in_schema = include_in_schema
        self.openapi_extra = openapi_extra or {}
        self.security = security  # None = auto-derive; [] = disable; non-empty = override
        self.callbacks = callbacks or []
        # Per-operation servers / externalDocs (OpenAPI 3.1)
        self.servers = servers  # None = inherit from app
        self.external_docs = external_docs

        # Generate operation_id using the provided function or explicit
        # value. FA's signature is ``generate_unique_id_function(route)``
        # — single argument, returns a string.
        if operation_id is not None:
            self.operation_id = operation_id
        elif generate_unique_id_function is not None:
            try:
                self.operation_id = generate_unique_id_function(self)
            except TypeError:
                # Fall back to legacy ``(route, method)`` callers.
                self.operation_id = generate_unique_id_function(
                    self, self.methods[0] if self.methods else "get"
                )
        else:
            self.operation_id = None
        self.generate_unique_id_function = generate_unique_id_function


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
        **kwargs: Any,
    ):
        self.routes: list[APIRoute] = []
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
        from fastapi_rs.dependencies import Depends as _Depends
        from fastapi_rs.exceptions import FastAPIError as _FE

        def _marker_scope(marker) -> str:
            s = getattr(marker, "scope", None)
            return s if s in ("function", "request") else "request"

        def _collect_depends(fn):
            """Yield (param_name, Depends marker) for every Depends on fn's sig."""
            try:
                sig = _inspect.signature(fn)
            except (TypeError, ValueError):
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
        from fastapi_rs.param_functions import _ParamMarker as _PM
        from fastapi_rs.dependencies import Depends as _Dep

        import re as _re
        path_param_names = set(_re.findall(r"\{([^}:]+)", path))
        try:
            sig = _inspect.signature(endpoint)
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
            sig = _inspect.signature(endpoint)
        except (TypeError, ValueError):
            return
        from fastapi_rs.param_functions import Form as _Form, File as _File
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
            sig = _inspect.signature(endpoint)
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
    def _assert_response_models_are_valid(kwargs: dict) -> None:
        """FA raises ``FastAPIError`` at decoration time when a
        ``response_model=`` or a ``responses={code: {"model": ...}}``
        references a non-Pydantic type. Mirror that behaviour.
        """
        from fastapi_rs.exceptions import FastAPIError as _FAErr
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
        from fastapi_rs.param_functions import Query as _Query

        try:
            sig = _inspect.signature(endpoint)
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
            if origin is _typing.Union:
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

    def route(self, path: str, methods: list[str] | None = None, **kwargs: Any):
        """Generic route decorator (Starlette-compatible).

        Usage::

            @router.route("/health", methods=["GET", "POST"])
            async def health(request): ...
        """

        def decorator(func: Callable) -> Callable:
            self.add_api_route(path, func, methods=methods, **kwargs)
            return func

        return decorator

    def add_route(
        self,
        path: str,
        endpoint: Callable,
        methods: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Imperative generic route registration (Starlette-compatible)."""
        self.add_api_route(path, endpoint, methods=methods, **kwargs)

    # ------------------------------------------------------------------
    # WebSocket routes
    # ------------------------------------------------------------------

    def add_websocket_route(
        self,
        path: str,
        endpoint: Callable,
        *,
        name: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Register a WebSocket route."""
        # FastAPI 0.120+ scope rule check. Raise FastAPIError at
        # decoration time when a request-scope yield-dep depends on a
        # function-scope yield-dep — matches FA parity so tests asserting
        # ``pytest.raises(FastAPIError)`` around the decorator fire.
        _ws_check_scope_mismatch(endpoint)
        route = APIRoute(
            path,
            endpoint,
            methods=["GET"],
            name=name,
            **kwargs,
        )
        # Tag it so the application layer can distinguish WS routes.
        route._is_websocket = True
        self.routes.append(route)

    def add_api_websocket_route(
        self,
        path: str,
        endpoint: Callable,
        *,
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
        include_meta = {
            "prefix": prefix,
            "tags": tags or [],
            "dependencies": list(dependencies or []),
            "responses": responses or {},
            "deprecated": deprecated,
            "include_in_schema": include_in_schema,
            "default_response_class": default_response_class,
            "generate_unique_id_function": generate_unique_id_function,
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

        def _mirror(src_router, pfx: str) -> None:
            for r in getattr(src_router, "routes", []):
                if getattr(r, "_is_included_shadow", False):
                    continue
                clone = _copy.copy(r)
                clone.path = _stack_path(pfx, getattr(r, "path", ""))
                clone._is_included_shadow = True
                self.routes.append(clone)
            for entry in getattr(src_router, "_included_routers", []):
                child_router, child_prefix = entry[0], entry[1]
                nested_prefix = _stack_path(
                    _stack_path(pfx, child_prefix or ""),
                    getattr(child_router, "prefix", "") or "",
                )
                _mirror(child_router, nested_prefix)

        _mirror(router, full_prefix)
