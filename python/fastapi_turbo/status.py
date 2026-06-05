"""HTTP / WebSocket status codes — re-exported from real ``starlette.status``.

The hand-written clone constants were byte-identical to Starlette's (same 82
``HTTP_*`` / ``WS_*`` names, same values — verified), so this is a pure
re-export. We copy by ``dir()`` rather than ``import *`` on purpose: Starlette
keeps four legacy 4xx aliases (``HTTP_413_REQUEST_ENTITY_TOO_LARGE``,
``HTTP_414_REQUEST_URI_TOO_LONG``, ``HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE``,
``HTTP_422_UNPROCESSABLE_ENTITY``) as module attributes but excludes them from
``__all__``, so ``import *`` would drop them and break code/tests that use the
legacy names.

Imported at ``__init__.py`` before ``compat.install()``, so ``starlette.status``
resolves to the REAL package; the bound names stay real after the shim runs.
"""
from starlette import status as _status

globals().update({_n: getattr(_status, _n) for _n in dir(_status) if _n.isupper()})

__all__ = [_n for _n in dir(_status) if _n.isupper()]
