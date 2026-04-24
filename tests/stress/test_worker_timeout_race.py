"""Regression: worker-timeout cancellation must win the race with
``_kickoff`` running on the worker loop thread.

Bug: ``submit()`` stored the ``asyncio.Task`` handle in ``box[2]`` *from
inside* ``_kickoff``, which is dispatched via
``loop.call_soon_threadsafe(_kickoff)``. When ``timeout=0.0`` (or any
value small enough that ``ev.wait`` returns False before ``_kickoff``
runs on the loop), the cancellation branch read ``task = box[2]``
saw ``None``, and silently skipped the ``task.cancel()``. Meanwhile
``_kickoff`` continued executing on the loop and the coroutine ran to
completion *after* ``TimeoutError`` was raised in the caller — a
genuine-silent-side-effect leak."""
from __future__ import annotations

import asyncio
import time

import pytest

import fastapi_turbo  # noqa: F401

from fastapi_turbo._async_worker import submit


def test_timeout_zero_does_not_allow_coroutine_to_complete():
    """Hardest form of the race: timeout=0.0 → ev.wait returns False
    immediately; if the cancel path is racy, the coroutine runs anyway."""
    ran_to_end = []

    async def slow():
        await asyncio.sleep(0.2)
        ran_to_end.append(True)
        return "leaked"

    with pytest.raises(TimeoutError):
        submit(slow(), timeout=0.0)

    # Give the worker loop plenty of time to drain a runaway task.
    time.sleep(0.4)
    assert ran_to_end == [], (
        "coroutine ran to completion after TimeoutError — cancel lost the race"
    )


def test_small_timeout_still_cancels_long_coroutine():
    """Less-pathological but more realistic: 10 ms timeout on a 500 ms
    coroutine. The cancel MUST fire and prevent the side-effect."""
    ran_to_end = []

    async def long():
        await asyncio.sleep(0.5)
        ran_to_end.append(True)

    with pytest.raises(TimeoutError):
        submit(long(), timeout=0.01)

    time.sleep(0.7)
    assert ran_to_end == []


def test_repeated_zero_timeouts_do_not_pile_up_zombie_tasks():
    """Back-to-back timeouts must not leave zombie coroutines running
    on the shared worker loop — otherwise they'd consume the loop's
    scheduling budget indefinitely."""
    ran_to_end = []

    async def slow(idx):
        await asyncio.sleep(0.15)
        ran_to_end.append(idx)

    for i in range(10):
        with pytest.raises(TimeoutError):
            submit(slow(i), timeout=0.0)

    time.sleep(0.4)
    assert ran_to_end == [], f"leaked {len(ran_to_end)} runaway coroutines"
