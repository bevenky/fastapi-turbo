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


def _run_background_obj(bg: Any, app: Any = None) -> None:
    """Door drain for ``Response.background`` (called from Rust
    ``responses.rs::py_to_response`` after the response is produced).

    Starlette runs ``await response.background()`` after sending; the door has
    no ASGI server awaiting it, and real Starlette's ``BackgroundTask.__call__``
    wraps SYNC funcs in ``run_in_threadpool`` (requires a running anyio loop) —
    so a bare ``send(None)`` drive dies on the first suspend and the task
    silently never runs (R2 deep-behavior T0059). Mirror ``run_sync``'s
    semantics instead for every shape a user can attach:

    * our ``BackgroundTasks`` (has ``run_sync``) — drain via ``run_sync``;
    * real Starlette ``BackgroundTasks`` (has ``.tasks``) — per-task drain;
    * a single ``BackgroundTask`` (has ``.func``) — sync inline, async via the
      shared worker loop (loop affinity, same as ``run_sync``);
    * any bare callable — call it; drive a returned coroutine on the loop.
    """
    from fastapi_turbo._async_worker import submit

    if bg is None:
        return
    run_sync = getattr(bg, "run_sync", None)
    if callable(run_sync):
        if app is not None and getattr(bg, "_door_app", None) is None:
            try:
                bg._door_app = app
            except (AttributeError, TypeError):
                pass
        run_sync()
        return
    tasks = getattr(bg, "tasks", None)
    if tasks is not None and getattr(bg, "func", None) is None:
        for task in list(tasks):
            _run_background_obj(task, app=app)
        return
    func = getattr(bg, "func", None)
    if func is not None:
        args = getattr(bg, "args", ()) or ()
        kwargs = getattr(bg, "kwargs", None) or {}
        if getattr(bg, "is_async", False) or inspect.iscoroutinefunction(func):
            submit(func(*args, **kwargs), app=app)
        else:
            func(*args, **kwargs)
        return
    if callable(bg):
        result = bg()
        if inspect.iscoroutine(result):
            submit(result, app=app)
