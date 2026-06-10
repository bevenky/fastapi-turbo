"""BackgroundTasks — real Starlette base + the door's loop-affinity drain.

``BackgroundTask`` is re-exported from real Starlette. ``BackgroundTasks``
subclasses real ``starlette.background.BackgroundTasks`` and adds ONLY
``run_sync`` (plus the ``_tasks`` / ``_app`` names the Rust door + middleware
wrapper reference). Why not a pure re-export: the door drives tasks itself (no
ASGI server awaits ``__call__``), and must preserve async-task loop affinity
(Issue #1) — async tasks go through ``_async_worker.submit`` on the shared
worker loop, NOT real ``__call__``'s bare ``await`` (which would bind them to
whatever loop happens to await it).

Imported during ``fastapi_turbo`` package init BEFORE the compat shim rebinds
``sys.modules``, so this resolves to REAL Starlette.
"""
from __future__ import annotations

import inspect
from typing import Any

from starlette.background import BackgroundTask, BackgroundTasks as _RealBackgroundTasks

__all__ = ["BackgroundTask", "BackgroundTasks"]


class BackgroundTasks(_RealBackgroundTasks):
    """Real Starlette ``BackgroundTasks`` (``add_task`` / ``tasks`` /
    ``__call__`` inherited) plus the door's synchronous drain."""

    def __init__(self, tasks=None) -> None:
        super().__init__(tasks)  # real: self.tasks = list(tasks or [])
        # Set by the Rust router at injection time so async tasks submitted via
        # ``run_sync`` honour the owning app's worker loop / ``worker_timeout``.
        self._door_app: Any | None = None

    @property
    def _tasks(self):
        """Alias for the name the Rust door (router.rs ``drain_background_tasks``)
        and middleware wrapper read."""
        return self.tasks

    @property
    def _app(self):
        return self._door_app

    @_app.setter
    def _app(self, value):
        self._door_app = value

    def run_sync(self) -> None:
        """Run all queued tasks after the response — the door has no ASGI server
        to await ``__call__``. Sync tasks run inline; async tasks go to the
        shared worker loop so async DB / cache / HTTP clients keep their
        connection affinity (Issue #1)."""
        from fastapi_turbo._async_worker import submit

        for task in self.tasks:
            func = task.func
            if getattr(task, "is_async", False) or inspect.iscoroutinefunction(func):
                submit(func(*task.args, **task.kwargs), app=self._door_app)
            else:
                func(*task.args, **task.kwargs)
        self.tasks.clear()
