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
            from fastapi_rs._async_worker import submit
            return submit(async_func(**kwargs))
        _noawait_caller._fastapi_rs_wrapped_id = func_id
        return _noawait_caller

    if for_handler:
        def _submit_caller(**kwargs):
            from fastapi_rs._async_worker import submit
            return submit(async_func(**kwargs))
        _submit_caller._fastapi_rs_wrapped_id = func_id
        return _submit_caller

    needs_loop = [False]

    def _submit_partial(coro):
        import asyncio as _asyncio
        from fastapi_rs._async_worker import get_loop
        loop = get_loop()
        fut = _asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=30)

    def _sync_caller(**kwargs):
        if needs_loop[0]:
            from fastapi_rs._async_worker import submit
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
            from fastapi_rs._async_worker import submit
            return submit(async_func(**kwargs))
        needs_loop[0] = True
        return _submit_partial(coro)

    _sync_caller._fastapi_rs_wrapped_id = func_id
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
        # Prepend its scopes as we walk down — later pops match FA's
        # "outer wraps the inner" ordering.
        own_scopes = list(getattr(dep, "scopes", None) or [])
        chain_scopes = list(accumulated_scopes or [])
        if own_scopes:
            chain_scopes = own_scopes + chain_scopes

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
                _dp_scopes = list(dp.get("_sub_dep_scopes") or [])
                if _dp_scopes:
                    from fastapi_rs.dependencies import Security as _Sec
                    sub_dep = _Sec(
                        dp["dep_callable"],
                        scopes=_dp_scopes,
                        use_cache=dp.get("use_cache", True),
                    )
                else:
                    sub_dep = Depends(dp["dep_callable"], use_cache=dp.get("use_cache", True))
                source_key = _resolve_dep(
                    dp["name"], sub_dep, is_top_level=False,
                    accumulated_scopes=chain_scopes,
                )
                input_map.append((dp["name"], source_key))
            else:
                source_key = _ensure_extraction(dp)
                input_map.append((dp["name"], source_key))

        result_key = param_name
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
            "dep_callable_id": func_id,
            "is_async_dep": mark_as_async,
            "is_generator_dep": is_generator,
            "dep_input_map": input_map,
            "use_cache": dep.use_cache,
            # Cumulative ``Security(..., scopes=[...])`` values along
            # the path from route to this dep. Empty for plain
            # ``Depends()`` chains.
            "_security_scopes": chain_scopes,
            "_security_scopes_param": _sec_scopes_param,
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
            dep = Depends(p["dep_callable"], use_cache=p.get("use_cache", True))
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
