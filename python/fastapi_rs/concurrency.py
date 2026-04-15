"""Concurrency utilities matching ``starlette.concurrency``."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any, Callable, TypeVar

T = TypeVar("T")


async def run_in_threadpool(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a sync function in a thread pool executor.

    Equivalent to ``starlette.concurrency.run_in_threadpool``.
    """
    loop = asyncio.get_running_loop()
    if kwargs:
        func = partial(func, **kwargs)  # type: ignore[assignment]
    return await loop.run_in_executor(None, func, *args)


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
