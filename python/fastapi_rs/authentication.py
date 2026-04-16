"""Authentication primitives (Starlette-compatible).

Provides:
  - BaseUser / SimpleUser / UnauthenticatedUser — user objects
  - AuthCredentials — scope container
  - AuthenticationBackend — protocol for auth backends
  - AuthenticationMiddleware — wires backends into the request scope
  - requires() decorator for scope-based authorization

Usage::

    from fastapi_rs.authentication import (
        AuthenticationBackend, AuthCredentials, SimpleUser, requires
    )

    class TokenAuth(AuthenticationBackend):
        async def authenticate(self, request):
            token = request.headers.get("authorization")
            if not token:
                return None
            user = resolve_token(token)
            return AuthCredentials(["authenticated"]), SimpleUser(user.username)

    app.add_middleware(AuthenticationMiddleware, backend=TokenAuth())

    @app.get("/me")
    @requires("authenticated")
    async def me(request: Request):
        return {"user": request.user.username}
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable


# ── User objects ────────────────────────────────────────────────────


class BaseUser:
    """Base class for authenticated users. Matches Starlette's BaseUser."""

    @property
    def is_authenticated(self) -> bool:
        raise NotImplementedError

    @property
    def display_name(self) -> str:
        raise NotImplementedError

    @property
    def identity(self) -> str:
        raise NotImplementedError


class SimpleUser(BaseUser):
    """Simple authenticated user with a username."""

    def __init__(self, username: str):
        self.username = username

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def display_name(self) -> str:
        return self.username

    @property
    def identity(self) -> str:
        return self.username

    def __bool__(self) -> bool:
        return True

    def __repr__(self) -> str:
        return f"SimpleUser(username={self.username!r})"


class UnauthenticatedUser(BaseUser):
    """Sentinel for unauthenticated requests. Evaluates falsy."""

    @property
    def is_authenticated(self) -> bool:
        return False

    @property
    def display_name(self) -> str:
        return ""

    @property
    def identity(self) -> str:
        return ""

    def __bool__(self) -> bool:
        return False


# ── Auth credentials ────────────────────────────────────────────────


class AuthCredentials:
    """Container for a request's granted scopes (strings)."""

    def __init__(self, scopes: list[str] | None = None):
        self.scopes: list[str] = list(scopes or [])

    def __repr__(self) -> str:
        return f"AuthCredentials(scopes={self.scopes!r})"


# ── Auth backend protocol ───────────────────────────────────────────


class AuthenticationBackend:
    """Base class for authentication backends.

    Implement ``authenticate(request)`` returning either:
      - None: no credentials found; request is unauthenticated
      - Tuple of (AuthCredentials, BaseUser): authenticated request
    """

    async def authenticate(self, request) -> tuple[AuthCredentials, BaseUser] | None:
        raise NotImplementedError


class AuthenticationError(Exception):
    """Raised by backends to signal an auth failure."""


# ── Middleware ──────────────────────────────────────────────────────


class AuthenticationMiddleware:
    """HTTP middleware that runs an AuthenticationBackend per request.

    Populates request.scope['auth'] (AuthCredentials) and
    request.scope['user'] (BaseUser). User code can then read
    ``request.user`` and ``request.auth`` normally.

    Usable via app.add_middleware(AuthenticationMiddleware, backend=MyBackend())
    OR via @app.middleware('http') manually.
    """

    _fastapi_rs_middleware_type = "python_http_auth"

    def __init__(self, app=None, backend: AuthenticationBackend | None = None,
                 on_error: Callable | None = None):
        self.app = app
        self.backend = backend
        self.on_error = on_error or _default_on_error

    async def __call__(self, request, call_next):
        if self.backend is None:
            return await call_next(request)
        try:
            result = await self.backend.authenticate(request)
        except AuthenticationError as exc:
            return self.on_error(request, exc)
        if result is not None:
            creds, user = result
            request.scope["auth"] = creds
            request.scope["user"] = user
        return await call_next(request)


def _default_on_error(request, exc: AuthenticationError):
    from fastapi_rs.responses import JSONResponse
    return JSONResponse({"detail": str(exc)}, status_code=401)


# ── requires() decorator for scope-based authorization ──────────────


def requires(scopes: str | list[str], status_code: int = 403, redirect: str | None = None):
    """Decorator enforcing that the request has one or more auth scopes.

    Matches Starlette's @requires — the decorated endpoint receives a
    ``request`` parameter. If the request lacks any required scope, either:
      - redirect to ``redirect`` URL, or
      - return ``status_code`` (default 403).

    Usage::

        @app.get("/admin")
        @requires(["authenticated", "admin"])
        async def admin_page(request: Request):
            return {"ok": True}
    """
    scope_list = [scopes] if isinstance(scopes, str) else list(scopes)

    def decorator(func: Callable) -> Callable:
        is_async = inspect.iscoroutinefunction(func)

        if is_async:
            @functools.wraps(func)
            async def wrapper(*args, **kwargs):
                request = _find_request(args, kwargs)
                if not _has_scopes(request, scope_list):
                    return _unauthorized(request, status_code, redirect)
                return await func(*args, **kwargs)
            return wrapper
        else:
            @functools.wraps(func)
            def wrapper_sync(*args, **kwargs):
                request = _find_request(args, kwargs)
                if not _has_scopes(request, scope_list):
                    return _unauthorized(request, status_code, redirect)
                return func(*args, **kwargs)
            return wrapper_sync

    return decorator


def _find_request(args: tuple, kwargs: dict) -> Any:
    """Locate the Request object among args/kwargs."""
    from fastapi_rs.requests import Request

    for a in args:
        if isinstance(a, Request):
            return a
    for v in kwargs.values():
        if isinstance(v, Request):
            return v
    raise RuntimeError("@requires used on a handler without a Request parameter")


def _has_scopes(request, required: list[str]) -> bool:
    auth = request.scope.get("auth")
    if auth is None:
        return False
    granted = set(getattr(auth, "scopes", []))
    return all(s in granted for s in required)


def _unauthorized(request, status_code: int, redirect: str | None):
    if redirect:
        from fastapi_rs.responses import RedirectResponse
        return RedirectResponse(redirect, status_code=303)
    from fastapi_rs.responses import JSONResponse
    return JSONResponse({"detail": "Forbidden"}, status_code=status_code)
