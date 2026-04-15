"""BaseHTTPMiddleware for fastapi-rs.

Provides a ``dispatch(request, call_next)`` interface similar to
Starlette's BaseHTTPMiddleware. Since the Rust/Tower layer cannot be
intercepted from Python, this works by wrapping route handlers at the
Python level.

For usage with ``app.add_middleware()``, the middleware's ``dispatch``
method is stored and invoked as a Python-level pre/post-processing step
around the handler.
"""

from __future__ import annotations

from typing import Any, Callable


class BaseHTTPMiddleware:
    """Starlette-compatible BaseHTTPMiddleware.

    Subclass this and override ``dispatch(request, call_next)`` to run
    logic before/after each request handler.
    """

    _fastapi_rs_middleware_type = "base_http"

    def __init__(self, app: Any = None, dispatch: Callable | None = None):
        self.app = app
        self.dispatch_func = dispatch or self.dispatch

    async def dispatch(self, request, call_next):
        """Override this to add middleware logic.

        ``call_next`` accepts the request and returns a Response.
        """
        raise NotImplementedError(
            "Override the dispatch method in your BaseHTTPMiddleware subclass"
        )

    async def __call__(self, request, call_next):
        return await self.dispatch_func(request, call_next)
