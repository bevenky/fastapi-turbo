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
