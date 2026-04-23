"""BackgroundTasks for running tasks after response is sent."""

from __future__ import annotations

import inspect
from typing import Any, Callable


class BackgroundTask:
    """A single background task."""

    def __init__(self, func: Callable, *args: Any, **kwargs: Any):
        self.func = func
        self.args = args
        self.kwargs = kwargs

    async def __call__(self) -> None:
        if inspect.iscoroutinefunction(self.func):
            await self.func(*self.args, **self.kwargs)
        else:
            self.func(*self.args, **self.kwargs)


class BackgroundTasks:
    """Container for multiple background tasks to run after the response.

    Compatible with FastAPI's ``BackgroundTasks`` dependency:

        @app.post("/send")
        async def send_email(background_tasks: BackgroundTasks):
            background_tasks.add_task(send_email_task, "user@example.com")
            return {"message": "sent"}
    """

    def __init__(self) -> None:
        self._tasks: list[BackgroundTask] = []

    def add_task(self, func: Callable, *args: Any, **kwargs: Any) -> None:
        """Add a function to be called in the background after response."""
        self._tasks.append(BackgroundTask(func, *args, **kwargs))

    async def _run(self) -> None:
        """Execute all queued background tasks."""
        for task in self._tasks:
            await task()

    async def __call__(self) -> None:
        await self._run()

    def run_sync(self) -> None:
        """Run all queued tasks synchronously — used by the Rust router
        after the handler returns. Sync tasks run inline; async tasks get
        submitted to the shared worker event loop so connection pools
        (SQLAlchemy asyncpg, Redis async, httpx) keep their affinity.
        """
        for task in self._tasks:
            if inspect.iscoroutinefunction(task.func):
                # Submit to the shared worker loop (same loop handles all
                # async deps and request handlers) so async DB / cache /
                # HTTP clients reuse their existing connections.
                from fastapi_turbo._async_worker import submit
                submit(task.func(*task.args, **task.kwargs))
            else:
                task.func(*task.args, **task.kwargs)
        self._tasks.clear()
