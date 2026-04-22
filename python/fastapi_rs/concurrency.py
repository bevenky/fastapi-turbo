"""Concurrency utilities matching ``starlette.concurrency``."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any, Callable, TypeVar

T = TypeVar("T")


async def run_in_threadpool(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a sync function in a thread pool executor.

    Equivalent to ``starlette.concurrency.run_in_threadpool``.

    When called from a handler driven via the sync fast path (coro.send(None)),
    there is no running event loop. In that case, call the function directly —
    we are already in a blocking thread, so running sync code is safe and avoids
    the "no running event loop" RuntimeError.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running event loop — we're already in a blocking thread, just call directly
        if kwargs:
            return func(*args, **kwargs)
        return func(*args)
    if kwargs:
        func = partial(func, **kwargs)  # type: ignore[assignment]
    # uvloop on Python 3.14 internally calls the deprecated
    # ``asyncio.iscoroutinefunction`` during ``run_in_executor``. When the
    # test runner has ``filterwarnings = ["error"]`` that becomes a crash
    # in user code. Suppress that specific DeprecationWarning here — our
    # caller is using ``run_in_executor`` correctly.
    import warnings as _warnings
    with _warnings.catch_warnings():
        _warnings.filterwarnings(
            "ignore",
            category=DeprecationWarning,
            message=r".*asyncio\.iscoroutinefunction.*",
        )
        return await loop.run_in_executor(None, func, *args)


async def run_until_first_complete(
    *args: tuple[Callable[..., Any], dict[str, Any]],
) -> list[tuple[Any, Any]]:
    """Run multiple async functions, return when the first completes.

    Each positional argument is a ``(callable, kwargs)`` tuple.  All callables
    are started concurrently; when the first one finishes, the remaining tasks
    are cancelled.

    Returns a list of ``(task, result_or_None)`` for the completed tasks.
    Matches ``starlette.concurrency.run_until_first_complete``.
    """
    tasks = [asyncio.ensure_future(func(**kwargs)) for func, kwargs in args]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    return [
        (task, task.result() if task.done() and not task.cancelled() else None)
        for task in done
    ]


async def iterate_in_threadpool(iterator):
    """Wrap a sync iterator to yield from a thread pool."""

    class _StopIteration(Exception):
        pass

    def _next(it):
        try:
            return next(it)
        except StopIteration:
            raise _StopIteration()

    while True:
        try:
            yield await run_in_threadpool(_next, iterator)
        except _StopIteration:
            break
