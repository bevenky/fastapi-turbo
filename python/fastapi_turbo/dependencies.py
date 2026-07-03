"""Dependency injection markers, bridged to real FastAPI.

``Depends`` / ``Security`` ARE real FastAPI's ``fastapi.params.Depends`` /
``Security`` so that REAL FastAPI introspection (the pivot adapter, which builds a
real ``route.dependant``) recognizes them — while the clone's own
``isinstance(x, Depends)`` checks keep working (real ``Security`` IS-A real
``Depends``, same as before). Both carry ``.dependency`` / ``.use_cache`` /
``.scope`` (and ``Security.scopes``), the only attributes the clone reads.

This module is imported during ``fastapi_turbo`` package init, BEFORE
``compat.install()`` patches attributes onto the real packages, so
``import fastapi.params`` here resolves to the genuine module and the bound
references below stay genuine afterwards (``fastapi.params`` itself is never
patched).
"""

from __future__ import annotations

import fastapi.params as _real_params

# Real FastAPI's markers — recognized by both real introspection and the clone.
Depends = _real_params.Depends
Security = _real_params.Security

__all__ = ["Depends", "Security"]
