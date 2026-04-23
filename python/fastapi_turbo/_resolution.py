"""Build flat, topologically-sorted resolution plans for route handlers.

At startup (once per route) this module:
1. Introspects the handler to find its parameters
2. Recursively introspects any Depends() callables
3. Topologically sorts all steps into a flat execution plan
4. Returns the plan; the Rust side handles caching by dep_callable_id

The Rust side receives this flat list and executes it sequentially.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from fastapi_turbo._introspect import introspect_endpoint
from fastapi_turbo.dependencies import Depends


def _has_await_in_source(func) -> bool:
    """Best-effort static check: does this function's source text
    contain any ``await`` expressions? Used to classify async
    functions that are async-in-name-only (common FA pattern — users
    mark a handler ``async def`` out of habit, never actually await).

    Returns True on any detection failure so greenlet-bridge libs
    (SQLAlchemy async, redis.asyncio) fall through to the safe path.
    """
    try:
        import ast
        import inspect as _inspect
        src = _inspect.getsource(func)
        # De-indent so getsource on a class/module method still parses.
        import textwrap
        tree = ast.parse(textwrap.dedent(src))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Await, ast.AsyncFor, ast.AsyncWith)):
                return True
        return False
    except Exception:  # noqa: BLE001
        return True


def _make_sync_wrapper(async_func, *, for_handler: bool = False):
    """Wrap an async function so it can be called from sync code.

    Three execution paths:

    * **Async-in-name-only fast path** — if a static AST scan sees no
      ``await``/``async for``/``async with`` anywhere in the function,
      we drive the coroutine with a single ``send(None)`` on the
      calling thread. Used for both deps and handlers that are
      ``async def`` only by convention. Costs ~2 µs.

    * **Dep fast path** (``for_handler=False``) — try ``send(None)``
      once. If the coroutine raises ``StopIteration`` we're done. If
      it suspends, continue the *partially-started* coroutine on the
      shared worker loop via ``run_coroutine_threadsafe`` (no double
      execution). If it raises anything else, fall back to submitting
      a fresh coroutine on the worker loop. After first suspension we
      flip a per-function flag so subsequent calls go straight to the
      worker loop — greenlet-bridge libs need loop affinity from the
      first instruction.

    * **Handler safe path** (``for_handler=True``) — always submit to
      the worker loop. Request handlers commonly touch greenlet-bridged
      libraries (SQLAlchemy async, redis.asyncio) whose connection
      pools bind to the thread that created them; trying the fast path
      risks a greenlet thread-switch error on the first await.
    """
    func_id = id(async_func)

    # If the function never awaits, both deps AND handlers can run on
    # the calling thread with a single send(None) — 0.5µs vs 30µs for
    # the worker-loop round-trip.
    if not _has_await_in_source(async_func):
        def _noawait_caller(**kwargs):
            coro = async_func(**kwargs)
            try:
                coro.send(None)
            except StopIteration as e:
                return e.value
            except BaseException:
                try:
                    coro.close()
                except Exception:  # noqa: BLE001
                    pass
                raise
            # AST lied — rare but possible (e.g. ``await`` inside a
            # nested function that IS called). Flip to slow path.
            try:
                coro.close()
            except Exception:  # noqa: BLE001
                pass
            from fastapi_turbo._async_worker import submit
            return submit(async_func(**kwargs))
        _noawait_caller._fastapi_turbo_wrapped_id = func_id
        return _noawait_caller

    if for_handler:
        def _submit_caller(**kwargs):
            from fastapi_turbo._async_worker import submit
            return submit(async_func(**kwargs))
        _submit_caller._fastapi_turbo_wrapped_id = func_id
        return _submit_caller

    needs_loop = [False]

    def _submit_partial(coro):
        import asyncio as _asyncio
        from fastapi_turbo._async_worker import get_loop
        loop = get_loop()
        fut = _asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=30)

    def _sync_caller(**kwargs):
        if needs_loop[0]:
            from fastapi_turbo._async_worker import submit
            return submit(async_func(**kwargs))

        coro = async_func(**kwargs)
        try:
            coro.send(None)
        except StopIteration as e:
            return e.value
        except BaseException:
            needs_loop[0] = True
            try:
                coro.close()
            except Exception:  # noqa: BLE001
                pass
            from fastapi_turbo._async_worker import submit
            return submit(async_func(**kwargs))
        needs_loop[0] = True
        return _submit_partial(coro)

    _sync_caller._fastapi_turbo_wrapped_id = func_id
    return _sync_caller


def _callable_uses_scopes(
    call: Any,
    path: str,
    _seen: set[int] | None = None,
) -> bool:
    """Does ``call`` (or any of its transitive sub-deps) use OAuth2 scopes?

    Mirrors FA's ``Dependant._uses_scopes``. True when:
      * the dep is a ``SecurityBase`` subclass instance (any scheme), OR
      * the dep takes ``SecurityScopes`` as a parameter, OR
      * any sub-dep has own ``Security(..., scopes=[...])`` scopes, OR
      * any sub-dep transitively uses scopes.

    Used to key the dep cache: only scope-using deps get separate
    cache entries when the accumulated scope chain differs; plain
    helpers like ``get_db_session`` stay shared even when pulled
    through two different Security chains.
    """
    if call is None:
        return False
    if _seen is None:
        _seen = set()
    cid = id(call)
    if cid in _seen:
        return False
    _seen.add(cid)

    # Is this callable itself a SecurityBase instance?
    try:
        from fastapi_turbo.security import (
            HTTPBase as _HTTPBase,
            OAuth2 as _OAuth2,
            OAuth2PasswordBearer as _O2PB,
            OAuth2AuthorizationCodeBearer as _O2ACB,
            OAuth2ClientCredentials as _O2CC,
            OpenIdConnect as _OIDC,
        )
        from fastapi_turbo.security import _APIKeyBase  # type: ignore
        _security_base_types: tuple = (
            _HTTPBase, _OAuth2, _O2PB, _O2ACB, _O2CC, _OIDC, _APIKeyBase,
        )
    except Exception:  # noqa: BLE001
        _security_base_types = ()
    try:
        if _security_base_types and isinstance(call, _security_base_types):
            return True
    except Exception:  # noqa: BLE001
        pass

    # Introspect its params.
    try:
        params = introspect_endpoint(call, path)
    except Exception:  # noqa: BLE001
        return False
    for dp in params:
        if dp.get("kind") == "inject_security_scopes":
            return True
        if dp.get("kind") == "dependency":
            if dp.get("_sub_dep_scopes"):
                return True
            sub_call = dp.get("dep_callable")
            if sub_call is not None and _callable_uses_scopes(sub_call, path, _seen):
                return True
    return False


def _propagate_scopes_to_descendants(
    dep_steps: list[dict[str, Any]],
    start_key: str,
    new_scopes: list[str],
) -> None:
    """Union ``new_scopes`` into the ``_security_scopes`` of every dep
    step reachable via ``dep_input_map`` from ``start_key``. Used when a
    cached sub-dep receives a larger scope set from a later
    ``Security()`` wrapper — without this, a scheme buried inside a
    ``get_token`` wrapper keeps the scopes of whichever ``Depends()``
    resolved it first.
    """
    by_name = {s.get("name"): s for s in dep_steps if s.get("name")}
    visited: set[str] = set()
    stack = [start_key]
    while stack:
        nm = stack.pop()
        if nm in visited:
            continue
        visited.add(nm)
        step = by_name.get(nm)
        if step is None:
            continue
        existing = list(step.get("_security_scopes") or [])
        for s in new_scopes:
            if s not in existing:
                existing.append(s)
        step["_security_scopes"] = existing
        for _, src_key in step.get("dep_input_map", []) or []:
            stack.append(src_key)


def build_resolution_plan(endpoint, path: str, extra_deps=None) -> list[dict[str, Any]]:
    """Build a flat, topologically-sorted execution plan for a route handler.

    Returns a flat list of steps (extractions first, then dep calls in topo order).

    Each step dict has at least:
        - name: result key for this step
        - kind: "path" | "query" | "header" | "cookie" | "body" | "dependency"

    Dependency steps additionally carry:
        - dep_callable: the function to call
        - dep_callable_id: id(dep_callable), for cache lookup
        - is_async_dep: whether the callable is async
        - is_generator_dep: whether the callable uses yield
        - dep_input_map: list of (dep_param_name, source_result_key) tuples
        - use_cache: whether to cache this dep's result

    Parameters
    ----------
    extra_deps : list, optional
        Additional Depends markers from app/router/route-level dependencies.
        These are resolved but not passed as handler params.
    """
    top_params = introspect_endpoint(endpoint, path)

    # Collect all steps: extraction steps first, then dependency steps
    extraction_steps: list[dict[str, Any]] = []
    dep_steps: list[dict[str, Any]] = []

    # Track extraction steps by canonical key for dedup
    extraction_keys: dict[str, str] = {}  # canonical_key -> step name

    # For recursive dep resolution, track which dep functions have been
    # fully resolved (their sub-deps processed). We still create a step
    # for every handler-level usage, but sub-deps are deduplicated.
    # Key: ``(id(callable), scope_tuple)`` — scope_tuple is ``()`` for
    # deps that don't transitively use OAuth2 scopes, or the sorted
    # tuple of accumulated scopes otherwise. This matches FA's
    # ``Dependant.cache_key`` which incorporates scopes only when the
    # dep (or a descendant) actually depends on them, so a plain helper
    # like ``get_db_session`` still dedupes across different
    # ``Security(..., scopes=[...])`` chains.
    sub_dep_result_keys: dict[tuple, str] = {}
    # Cache ``_callable_uses_scopes`` — computing it per dep is O(tree).
    _uses_scopes_cache: dict[int, bool] = {}

    def _ensure_extraction(dp: dict[str, Any]) -> str:
        """Ensure an extraction step exists, return its result key name."""
        canon = f"{dp['kind']}:{dp.get('alias') or dp['name']}"
        if canon in extraction_keys:
            return extraction_keys[canon]

        extraction_steps.append(dp)
        extraction_keys[canon] = dp["name"]
        return dp["name"]

    def _dep_scope(dep: Depends) -> str:
        """Effective scope for ``dep``. Default is ``"request"`` —
        FastAPI 0.120+ introduced ``Depends(..., scope="function" | "request")``,
        with request being the legacy default (teardown after response).
        """
        _s = getattr(dep, "scope", None)
        return _s if _s in ("function", "request") else "request"

    def _resolve_dep(
        param_name: str,
        dep: Depends,
        is_top_level: bool = False,
        accumulated_scopes: list[str] | None = None,
    ) -> str:
        """Recursively resolve a dependency, returning the result key name.

        For sub-dependencies (not top-level), deduplicates by func id when
        use_cache is True. For top-level deps, always creates a step so each
        handler param gets its own result key.

        ``accumulated_scopes`` tracks the full list of
        ``Security(..., scopes=[...])`` values encountered on the path
        from the route to this dep. Used to populate ``SecurityScopes``
        when the dep declares one as a parameter (FA parity).
        """
        # ``Security`` is a ``Depends`` subclass with a ``.scopes`` attr.
        # APPEND own scopes to the accumulated outer chain — FA's
        # ``security`` list emits outer-first, inner-last
        # (``Security(outer, scopes=["items"])`` wrapping
        # ``Security(inner, scopes=["me"])`` → ``["items", "me"]``).
        own_scopes = list(getattr(dep, "scopes", None) or [])
        chain_scopes = list(accumulated_scopes or [])
        if own_scopes:
            chain_scopes = chain_scopes + own_scopes

        dep_func = dep.dependency
        func_id = id(dep_func)

        # Compute whether this dep (or any sub-dep) uses scopes — only
        # those get scope-aware cache keys. Plain helpers share across
        # different scope chains. FA semantics: a ``Security(dep,
        # scopes=[...])`` marker with scopes on THIS level also counts,
        # even if the callable itself never reads them. Memoised per
        # callable for the recursive part.
        if func_id in _uses_scopes_cache:
            _uses = _uses_scopes_cache[func_id]
        else:
            _uses = _callable_uses_scopes(dep_func, path)
            _uses_scopes_cache[func_id] = _uses
        # ``own_scopes`` captured above from the marker — if set, this
        # call-site uses scopes even if the callable doesn't. At
        # top-level, the marker's scopes arrive via
        # ``accumulated_scopes`` (the caller repackages Security()
        # into Depends() and hands its scopes in separately), so also
        # check those — without it ``Security(dep, scopes=["s"])`` at
        # the handler level wouldn't be treated as a scope-using site.
        _site_uses = (
            _uses
            or bool(own_scopes)
            or (is_top_level and bool(accumulated_scopes))
        )
        _scope_key = tuple(sorted(set(chain_scopes))) if _site_uses else ()
        _dedup_key = (func_id, _scope_key)

        # For sub-deps (not top-level), deduplicate
        if not is_top_level and dep.use_cache and _dedup_key in sub_dep_result_keys:
            # Dedup hit — but propagate scopes forward. FA semantics:
            # when two different Security() chains resolve to the same
            # scheme, the scopes UNION. Without this, a dep resolved
            # via a ``Depends()`` (no scopes) followed by the same dep
            # via ``Security(..., scopes=[...])`` keeps the first
            # (empty) scope list forever.
            cached_key = sub_dep_result_keys[_dedup_key]
            if chain_scopes:
                # Locate the cached step and union scopes.
                for _ds in dep_steps:
                    if _ds.get("name") == cached_key:
                        _existing = list(_ds.get("_security_scopes") or [])
                        for _s in chain_scopes:
                            if _s not in _existing:
                                _existing.append(_s)
                        _ds["_security_scopes"] = _existing
                        # Propagate to sub-deps of this cached dep too,
                        # so scheme-bearing leaves inherit the scopes.
                        _propagate_scopes_to_descendants(
                            dep_steps, cached_key, chain_scopes,
                        )
                        break
            return cached_key

        # Introspect the dependency function's own parameters
        dep_params = introspect_endpoint(dep_func, path)

        # Build input map for this dep
        input_map: list[tuple[str, str]] = []

        for dp in dep_params:
            if dp["kind"] == "dependency":
                _dp_scopes = list(dp.get("_sub_dep_scopes") or [])
                _sub_marker_scope = dp.get("_dep_scope") or "request"
                if _dp_scopes:
                    from fastapi_turbo.dependencies import Security as _Sec
                    sub_dep = _Sec(
                        dp["dep_callable"],
                        scopes=_dp_scopes,
                        use_cache=dp.get("use_cache", True),
                    )
                else:
                    sub_dep = Depends(
                        dp["dep_callable"],
                        use_cache=dp.get("use_cache", True),
                        scope=_sub_marker_scope,
                    )
                # FA 0.120+ scope rule: a ``request``-scope yield dep
                # cannot depend on a ``function``-scope yield dep.
                # Non-yield deps have no teardown so scope is a no-op.
                outer_scope = _dep_scope(dep)
                _outer_is_yield = (
                    inspect.isgeneratorfunction(dep_func)
                    or inspect.isasyncgenfunction(dep_func)
                )
                _sub_callable = dp.get("dep_callable")
                _sub_is_yield = (
                    _sub_callable is not None
                    and (
                        inspect.isgeneratorfunction(_sub_callable)
                        or inspect.isasyncgenfunction(_sub_callable)
                    )
                )
                if (
                    _outer_is_yield
                    and _sub_is_yield
                    and outer_scope == "request"
                    and _sub_marker_scope == "function"
                ):
                    from fastapi_turbo.exceptions import FastAPIError as _FE
                    _outer_name = getattr(dep_func, "__name__", repr(dep_func))
                    raise _FE(
                        f'The dependency "{_outer_name}" has a scope of "request", '
                        f'it cannot depend on dependencies with scope "function"'
                    )
                source_key = _resolve_dep(
                    dp["name"], sub_dep, is_top_level=False,
                    accumulated_scopes=chain_scopes,
                )
                input_map.append((dp["name"], source_key))
            else:
                source_key = _ensure_extraction(dp)
                input_map.append((dp["name"], source_key))

        result_key = param_name
        # Sub-dep steps must not collide with top-level handler-param
        # step names — the handler-kwarg builder looks up ``resolved[
        # param_name]``, so a sub-dep reusing the same name as a
        # handler param would silently overwrite the handler's value.
        # Before scope-aware caching this was papered over by the
        # func_id dedup (sub-dep and top-level shared a single step);
        # now that scope chains can split them into distinct steps
        # we disambiguate the sub-dep's result_key.
        if not is_top_level:
            _used_names = {s.get("name") for s in dep_steps if s.get("name")}
            _used_names |= {
                s.get("name") for s in extraction_steps if s.get("name")
            }
            # Collision with an already-planned step OR with an
            # as-yet-unplanned top-level handler param. Suffix with a
            # stable hash of ``(func_id, scope_key)`` so two sub-dep
            # occurrences of the same ``(func, scopes)`` still share.
            _top_names = {p.get("name") for p in top_params if p.get("name")}
            if result_key in _used_names or result_key in _top_names:
                _suffix = hex(hash((func_id, _scope_key)) & 0xFFFFFFFF)[2:]
                result_key = f"{param_name}__sd_{_suffix}"
                while result_key in _used_names:
                    result_key = f"{result_key}_"
        # A dep can be a bare function OR a callable class instance whose
        # `__call__` is async (e.g., Starlette's HTTPBearer / OAuth2*
        # classes). Check both cases.
        # Walk through @wraps-applied decorator chains (``__wrapped__``)
        # to detect the real underlying function — otherwise a
        # ``@noop_wrap``-wrapped generator dep looks like a plain
        # function and Rust calls ``.next(next(fn()))`` on a dict.
        _probe = dep_func
        for _ in range(10):  # defensive depth cap
            nxt = getattr(_probe, "__wrapped__", None)
            if nxt is None or nxt is _probe:
                break
            _probe = nxt

        # When the dep is a CLASS INSTANCE (``noop_wrap(some_instance)``),
        # ``__wrapped__`` may point at that instance. Also check the
        # instance's ``__call__`` for async/generator semantics.
        _probe_call = getattr(_probe, "__call__", None)
        _direct_call = getattr(dep_func, "__call__", None)

        # Also walk ``__wrapped__`` on the bound ``__call__`` itself —
        # a class instance with ``@noop_wrap`` decorating ``__call__``
        # has the wrapper chain attached to ``__call__`` (not the
        # instance), so the outer ``_probe`` loop never sees it.
        _unwrapped_call = _direct_call
        for _ in range(10):
            _nxt = getattr(_unwrapped_call, "__wrapped__", None)
            if _nxt is None or _nxt is _unwrapped_call:
                break
            _unwrapped_call = _nxt

        # When ``dep_func`` is a CLASS (``Depends(MyClass)`` /
        # ``instance: MyClass = Depends()``), calling it constructs an
        # instance — NOT a generator or coroutine — regardless of what
        # ``MyClass.__call__`` looks like. Only inspect ``__call__`` when
        # the dep is a callable INSTANCE.
        _is_dep_a_class = isinstance(dep_func, type)
        if _is_dep_a_class:
            is_async = False
            is_generator = False
        else:
            # ``functools.partial(f, ...)`` wraps the inner callable — walk
            # to the real function so its async/generator flags are seen.
            import functools as _ft
            _partial_probe = dep_func
            for _ in range(10):
                if isinstance(_partial_probe, _ft.partial):
                    _partial_probe = _partial_probe.func
                else:
                    break
            _partial_call = getattr(_partial_probe, "__call__", None)
            is_async = (
                inspect.iscoroutinefunction(dep_func)
                or inspect.iscoroutinefunction(_direct_call)
                or inspect.iscoroutinefunction(_probe)
                or inspect.iscoroutinefunction(_probe_call)
                or inspect.iscoroutinefunction(_partial_probe)
                or inspect.iscoroutinefunction(_partial_call)
                or inspect.iscoroutinefunction(_unwrapped_call)
            )
            is_generator = (
                inspect.isgeneratorfunction(dep_func)
                or inspect.isasyncgenfunction(dep_func)
                or inspect.isgeneratorfunction(_direct_call)
                or inspect.isasyncgenfunction(_direct_call)
                or inspect.isgeneratorfunction(_probe)
                or inspect.isasyncgenfunction(_probe)
                or inspect.isgeneratorfunction(_probe_call)
                or inspect.isasyncgenfunction(_probe_call)
                or inspect.isgeneratorfunction(_partial_probe)
                or inspect.isasyncgenfunction(_partial_probe)
                or inspect.isgeneratorfunction(_partial_call)
                or inspect.isasyncgenfunction(_partial_call)
                or inspect.isgeneratorfunction(_unwrapped_call)
                or inspect.isasyncgenfunction(_unwrapped_call)
            )

        # Optimization: wrap trivially-async deps in a sync caller.
        # This moves the coroutine protocol to Python (1 call) instead of
        # doing it through PyO3 (3 calls: create coro + send + catch StopIteration).
        actual_callable = dep_func
        mark_as_async = is_async
        if is_async and not is_generator:
            actual_callable = _make_sync_wrapper(dep_func)
            mark_as_async = False  # Rust treats it as sync now

        # Find any ``SecurityScopes`` param on this dep so we can
        # populate it with the accumulated scopes at runtime.
        _sec_scopes_param: str | None = None
        for _dp in dep_params:
            if _dp.get("kind") == "inject_security_scopes":
                _sec_scopes_param = _dp["name"]
                break

        dep_step = {
            "name": result_key,
            "kind": "dependency",
            "type_hint": "any",
            "required": False,
            "default_value": None,
            "model_class": None,
            "alias": None,
            "dep_callable": actual_callable,
            # Preserve the ORIGINAL dep (e.g., HTTPBearer instance) — the
            # OpenAPI generator uses this to find `.model` for
            # securitySchemes, and user dependency_overrides look up by id.
            "_original_dep_callable": dep_func,
            # Runtime cache key. For plain deps, this is just ``id(call)``
            # so the Rust-side ``dep_cache`` dedupes across different
            # handler-level Depends() of the same callable. For deps
            # that use OAuth2 scopes, the key also incorporates the
            # accumulated scope set — two ``Security()`` chains with
            # different scopes must produce separate cached values
            # (FA's ``Dependant.cache_key`` semantics).
            "dep_callable_id": (
                hash((func_id, _scope_key)) & 0xFFFFFFFFFFFFFFFF
                if _scope_key
                else func_id
            ),
            "is_async_dep": mark_as_async,
            "is_generator_dep": is_generator,
            "dep_input_map": input_map,
            "use_cache": dep.use_cache,
            # FA 0.120+ scope: teardown ordering marker. ``function`` runs
            # right after the handler; ``request`` defers until after the
            # streaming response completes. See _compiled for the split.
            "_dep_scope": _dep_scope(dep),
            # Cumulative ``Security(..., scopes=[...])`` values along
            # the path from route to this dep. Empty for plain
            # ``Depends()`` chains.
            "_security_scopes": chain_scopes,
            "_security_scopes_param": _sec_scopes_param,
        }
        dep_steps.append(dep_step)

        # Track for sub-dep dedup
        if dep.use_cache and _dedup_key not in sub_dep_result_keys:
            sub_dep_result_keys[_dedup_key] = result_key

        return result_key

    # Process extra (global/router/route-level) dependencies first (P0 fix #6)
    # These are resolved but NOT passed as handler params.
    if extra_deps:
        for i, dep_marker in enumerate(extra_deps):
            if isinstance(dep_marker, Depends):
                dep_name = f"_global_dep_{i}_{id(dep_marker.dependency)}"
                _resolve_dep(dep_name, dep_marker, is_top_level=True)

    # Process top-level handler params
    handler_param_names: set[str] = set()

    for p in top_params:
        if p.get("_is_param_model_builder"):
            # FA 0.115+ parameter-model synthetic builder: its
            # input_map was pre-wired to the expanded field
            # extraction steps, so we use it verbatim — running it
            # through the normal dep-resolver would introspect the
            # wrapper closure (no params) and drop the mapping.
            dep_steps.append(p)
            handler_param_names.add(p["name"])
        elif p["kind"] == "dependency":
            _top_scopes = list(p.get("_security_scopes_top") or [])
            dep = Depends(
                p["dep_callable"],
                use_cache=p.get("use_cache", True),
                scope=p.get("_dep_scope") or "request",
            )
            _resolve_dep(
                p["name"], dep, is_top_level=True,
                accumulated_scopes=_top_scopes,
            )
            handler_param_names.add(p["name"])
        else:
            _ensure_extraction(p)
            # Parameter-model synthetic extraction steps are NOT
            # handler kwargs — they flow into the builder. Keep
            # them out of ``handler_param_names`` so the compiled
            # wrapper doesn't hand them to the user handler.
            if not p.get("_param_model_field_name"):
                handler_param_names.add(p["name"])

    # When handler + sub-dep both declare body params with DIFFERENT names,
    # FA expects a combined top-level body: ``{"item": {...}, "item2": {...}}``.
    # After dep resolution, gather all body extraction steps and merge them
    # into a single ``_combined_body`` if more than one remains — mirrors
    # ``_maybe_embed_body_params`` but across the full resolution plan.
    body_steps = [
        s for s in extraction_steps
        if s.get("kind") == "body" and s.get("name") != "_combined_body"
    ]
    if len(body_steps) > 1:
        try:
            from pydantic import create_model
            from fastapi_turbo._introspect import _TypeAdapterProxy as _TAP
            field_definitions: dict[str, Any] = {}
            for bp in body_steps:
                model_cls = bp.get("model_class")
                if isinstance(model_cls, _TAP):
                    model_cls = model_cls._annotation
                if model_cls is not None:
                    if bp.get("required", True):
                        field_definitions[bp["name"]] = (model_cls, ...)
                    else:
                        field_definitions[bp["name"]] = (model_cls, bp.get("default_value"))
                else:
                    type_map = {"int": int, "float": float, "bool": bool, "str": str}
                    py_type = type_map.get(bp.get("type_hint", ""), Any)
                    field_definitions[bp["name"]] = (
                        py_type,
                        ... if bp.get("required", True) else bp.get("default_value"),
                    )
            _endpoint_name = getattr(endpoint, "__name__", "endpoint")
            CombinedBody = create_model(f"Body_{_endpoint_name}", **field_definitions)
            body_names = [bp["name"] for bp in body_steps]
            body_name_set = set(body_names)
            # Remove the individual body steps, add a single combined one.
            extraction_steps = [
                s for s in extraction_steps
                if not (s.get("kind") == "body" and s.get("name") in body_name_set)
            ]
            _combined_required = any(bp.get("required", True) for bp in body_steps)
            combined_step = {
                "name": "_combined_body",
                "kind": "body",
                "type_hint": "model",
                "required": _combined_required,
                "default_value": None,
                "has_default": not _combined_required,
                "model_class": CombinedBody,
                "alias": None,
                "_embed": False,
                "_body_param_names": body_names,
                "_is_handler_param": False,
                "_is_combined_body_for_deps": True,
            }
            extraction_steps.append(combined_step)

            # Rewire dep_input_maps: any ``body:<name>`` source now points
            # at ``_combined_body`` and the consumer needs to look up the
            # attribute off it. We encode this by marking the combined step
            # and letting ``_compiled`` split it before dep invocation.
            # Update input maps that reference individual body names.
            # Sub-deps referred to body params by name in input_map — since
            # the extraction key is ``dp["name"]`` which equals the body
            # param name, the maps stay intact (the consumer reads the
            # unpacked attr). We'll unpack in the compiled handler.
        except Exception:  # noqa: BLE001
            pass

    # The final plan: extractions first, then dep calls in topo order
    plan = extraction_steps + dep_steps

    # Mark handler params
    for step in plan:
        step["_is_handler_param"] = step["name"] in handler_param_names

    # Store original dep callable for override lookup — only set if the
    # step didn't already capture the original (e.g. when we wrapped an
    # async dep in a sync caller, `dep_callable` is the wrapper but the
    # original was stashed by `_resolve_dep` above).
    for step in plan:
        if step["kind"] == "dependency" and "_original_dep_callable" not in step:
            step["_original_dep_callable"] = step.get("dep_callable")

    return plan
