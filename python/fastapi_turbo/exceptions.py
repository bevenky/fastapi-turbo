"""Exception classes — re-exported from the REAL fastapi / starlette packages.

The installed FastAPI (0.136.0) already owns ``endpoint_ctx`` / ``EndpointContext``
/ ``_format_endpoint_context`` / the multi-line ``__str__`` that this project's
validation errors rely on — the previous hand-written clone classes were a copy
of exactly those. So the public names here ARE the real classes: ``isinstance``
checks, ``@app.exception_handler(...)`` keys, and the ``_lookup_exception_handler``
MRO walk all resolve identically whether an exception is raised by this framework
or (post-shim-flip) by real FastAPI / Starlette internals. The same holds for the
Rust door, which path-imports ``FastAPIError`` / ``WebSocketDisconnect`` from this
module (``router.rs`` / ``websocket.rs``).

The ONE behavioral delta is ``errors()``: the real classes return the error list
as-passed, but this engine's Rust validation extractor emits ``loc`` as a *list*
whereas pydantic (and therefore upstream FastAPI) emits it as a *tuple*. To keep
``exc.errors()`` byte-parity with upstream — handler code like
``err['loc'] == ('body', 'name')`` — the framework raises the thin ``_Door*``
subclasses below, which override ONLY ``errors()`` to coerce ``loc`` list→tuple
(a no-op for the paths that already build tuple ``loc``). Each ``_Door*`` IS-A its
real base, so handlers keyed on the real class still catch them (via the MRO walk)
and ``real in app.exception_handlers`` gates stay correct both before and after the
flip.

Imported during package init BEFORE the compat shim rebinds ``sys.modules`` (see
``__init__.py``), so these resolve to the REAL packages.
"""
from __future__ import annotations

from fastapi.exceptions import (
    FastAPIDeprecationWarning,
    FastAPIError,
    RequestErrorModel,
    RequestValidationError,
    ResponseValidationError,
    ValidationException,
    WebSocketErrorModel,
    WebSocketRequestValidationError,
)
from starlette.exceptions import HTTPException, WebSocketException
from starlette.formparsers import MultiPartException
from starlette.websockets import WebSocketDisconnect

__all__ = [
    "HTTPException",
    "WebSocketException",
    "WebSocketDisconnect",
    "MultiPartException",
    "FastAPIError",
    "FastAPIDeprecationWarning",
    "ValidationException",
    "RequestValidationError",
    "WebSocketRequestValidationError",
    "ResponseValidationError",
    "RequestErrorModel",
    "WebSocketErrorModel",
    "DependencyScopeError",
    "PydanticV1NotSupportedError",
    "_DoorRequestValidationError",
    "_DoorWebSocketRequestValidationError",
    "_DoorResponseValidationError",
]


class DependencyScopeError(FastAPIError):
    """Raised when a dependency is used outside its allowed scope (no real equivalent)."""


class PydanticV1NotSupportedError(FastAPIError):
    """Raised when a Pydantic v1 model is used where FastAPI requires v2 (no real equivalent)."""


def _loc_as_tuples(errors):
    """``loc`` list→tuple coercion. The Rust validation extractor emits ``loc`` as
    a list; pydantic / upstream FastAPI emit a tuple. Coerce so handler code like
    ``err['loc'] == ('body', 'name')`` matches upstream. A no-op for entries whose
    ``loc`` is already a tuple (the pydantic-sourced paths)."""
    return [
        {**e, "loc": tuple(e["loc"])} if isinstance(e.get("loc"), list) else e
        for e in errors
    ]


class _DoorRequestValidationError(RequestValidationError):
    """Framework-raised ``RequestValidationError`` with Rust-list ``loc`` coerced to
    tuple in ``errors()``. Everything else — the ``__init__`` signature accepting
    ``body`` / ``endpoint_ctx``, ``__str__``, the ``endpoint_*`` attrs — is the real
    base's."""

    def errors(self):
        return _loc_as_tuples(self._errors)


class _DoorWebSocketRequestValidationError(WebSocketRequestValidationError):
    """WS counterpart of :class:`_DoorRequestValidationError`."""

    def errors(self):
        return _loc_as_tuples(self._errors)


class _DoorResponseValidationError(ResponseValidationError):
    """Response-model counterpart of :class:`_DoorRequestValidationError`."""

    def errors(self):
        return _loc_as_tuples(self._errors)
