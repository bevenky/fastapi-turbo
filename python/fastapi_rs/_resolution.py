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

from fastapi_rs._introspect import introspect_endpoint
from fastapi_rs.dependencies import Depends


def _make_sync_wrapper(async_func):
    """Wrap an async function in a sync caller that drives the coroutine inline.

    For trivially-async functions (no real await), this completes synchronously
    via the StopIteration protocol — all in one Python call instead of 3 PyO3 calls.
    If the coroutine actually suspends, raises RuntimeError (fallback to event loop).
    """

    def _sync_caller(**kwargs):
        coro = async_func(**kwargs)
        try:
            coro.send(None)
        except StopIteration as e:
            return e.value
        else:
            coro.close()
            raise RuntimeError("async dep requires event loop")

    # Preserve the original function's identity for override lookup
    _sync_caller._fastapi_rs_wrapped_id = id(async_func)
    return _sync_caller


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
    sub_dep_result_keys: dict[int, str] = {}  # id(callable) -> result_key for sub-deps

    def _ensure_extraction(dp: dict[str, Any]) -> str:
        """Ensure an extraction step exists, return its result key name."""
        canon = f"{dp['kind']}:{dp.get('alias') or dp['name']}"
        if canon in extraction_keys:
            return extraction_keys[canon]

        extraction_steps.append(dp)
        extraction_keys[canon] = dp["name"]
        return dp["name"]

    def _resolve_dep(param_name: str, dep: Depends, is_top_level: bool = False) -> str:
        """Recursively resolve a dependency, returning the result key name.

        For sub-dependencies (not top-level), deduplicates by func id when
        use_cache is True. For top-level deps, always creates a step so each
        handler param gets its own result key.
        """
        dep_func = dep.dependency
        func_id = id(dep_func)

        # For sub-deps (not top-level), deduplicate
        if not is_top_level and dep.use_cache and func_id in sub_dep_result_keys:
            return sub_dep_result_keys[func_id]

        # Introspect the dependency function's own parameters
        dep_params = introspect_endpoint(dep_func, path)

        # Build input map for this dep
        input_map: list[tuple[str, str]] = []

        for dp in dep_params:
            if dp["kind"] == "dependency":
                sub_dep = Depends(dp["dep_callable"], use_cache=dp.get("use_cache", True))
                source_key = _resolve_dep(dp["name"], sub_dep, is_top_level=False)
                input_map.append((dp["name"], source_key))
            else:
                source_key = _ensure_extraction(dp)
                input_map.append((dp["name"], source_key))

        result_key = param_name
        # A dep can be a bare function OR a callable class instance whose
        # `__call__` is async (e.g., Starlette's HTTPBearer / OAuth2*
        # classes). Check both cases.
        is_async = (
            inspect.iscoroutinefunction(dep_func)
            or inspect.iscoroutinefunction(getattr(dep_func, "__call__", None))
        )
        is_generator = (
            inspect.isgeneratorfunction(dep_func)
            or inspect.isasyncgenfunction(dep_func)
        )

        # Optimization: wrap trivially-async deps in a sync caller.
        # This moves the coroutine protocol to Python (1 call) instead of
        # doing it through PyO3 (3 calls: create coro + send + catch StopIteration).
        actual_callable = dep_func
        mark_as_async = is_async
        if is_async and not is_generator:
            actual_callable = _make_sync_wrapper(dep_func)
            mark_as_async = False  # Rust treats it as sync now

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
            "dep_callable_id": func_id,
            "is_async_dep": mark_as_async,
            "is_generator_dep": is_generator,
            "dep_input_map": input_map,
            "use_cache": dep.use_cache,
        }
        dep_steps.append(dep_step)

        # Track for sub-dep dedup
        if dep.use_cache and func_id not in sub_dep_result_keys:
            sub_dep_result_keys[func_id] = result_key

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
        if p["kind"] == "dependency":
            dep = Depends(p["dep_callable"], use_cache=p.get("use_cache", True))
            _resolve_dep(p["name"], dep, is_top_level=True)
            handler_param_names.add(p["name"])
        else:
            _ensure_extraction(p)
            handler_param_names.add(p["name"])

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
